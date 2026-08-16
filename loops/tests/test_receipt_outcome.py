"""A receipt records the OUTCOME of an effect, not merely that it ran.

The live defect these tests exist for: ``receipts.guarded`` completed a receipt
holding W3's ``{"success": False, "returncode": 1}``, so one failed ``launchctl``
repair was filed as a done effect and the receipt then suppressed every retry for
the rest of the incident window (business key = component+signature+day). The
service stayed down while the loop reported the incident handled.

Every test here is written to fail on the pre-fix code. The bar is not "the new
functions work"; it is "would this have caught what shipped".
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import Counter, tool
from omniagentos_loops.contracts import (
    EffectAttemptsExhausted,
    EffectStateUnknown,
    LoopStatus,
    RiskTier,
)
from omniagentos_loops.receipts import (
    FAILED,
    SUCCEEDED,
    attempt_key,
    declared_failure,
    guarded,
    receipt_exists,
    receipt_key,
    receipt_state,
)
from omniagentos_loops.runtime import run_once
from omniagentos_loops.templates import get_template
from omniagentos_loops.tools import (
    DEFAULT_MAX_ATTEMPTS,
    MAX_ATTEMPTS_CEILING,
    LoopTool,
    ToolRegistry,
    Verification,
)


class Effect:
    """A callable whose per-call results are scripted. Counts every execution."""

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.results) - 1)
        return self.results[index]

    @property
    def count(self) -> int:
        return len(self.calls)


KICKSTART_FAILED = {"success": False, "returncode": 1, "stderr": "Could not find service"}
KICKSTART_OK = {"success": True, "label": "com.omni.api", "stdout": ""}


def _effect_tool(call: Any, **kwargs: Any) -> LoopTool:
    return LoopTool(
        name="effect",
        tier=kwargs.pop("tier", RiskTier.T1),
        idempotency_key=lambda args: "effect",
        call=call,
        description="test effect",
        **kwargs,
    )


def _ctx(make_ctx: Any, tool_obj: LoopTool) -> tuple[Any, LoopTool]:
    registry = ToolRegistry()
    registry.register(tool_obj)
    return make_ctx(tools=registry), registry.get(tool_obj.name)


def _key(ctx: Any) -> str:
    return receipt_key(ctx.instance_id, ctx.template, "node", "effect", "biz-1")


def _run(ctx: Any, tool_obj: LoopTool, execute: Any, **kwargs: Any) -> Any:
    return guarded(ctx, key=_key(ctx), node="node", tool=tool_obj, execute=execute, **kwargs)


# ---------------------------------------------------------------------------
# 1. a failed effect is recorded as failed, and does NOT suppress the next try
# ---------------------------------------------------------------------------


def test_a_failed_effect_is_recorded_failed_not_completed(make_ctx):
    """The exact live payload: the receipt must not say 'done'."""
    effect = Effect(KICKSTART_FAILED)
    ctx, tool_obj = _ctx(make_ctx, _effect_tool(effect))

    outcome = _run(ctx, tool_obj, effect)

    assert outcome.succeeded is False
    assert "returncode=1" in outcome.detail
    assert receipt_state(ctx, _key(ctx)) == FAILED
    assert not receipt_exists(ctx, _key(ctx)), "a failed effect is not a completed receipt"


def test_a_failed_receipt_does_not_suppress_the_next_attempt(make_ctx):
    """Pre-fix, call 2 replayed the failure dict and the effect never re-ran."""
    effect = Effect(KICKSTART_FAILED, KICKSTART_OK)
    ctx, tool_obj = _ctx(make_ctx, _effect_tool(effect))

    assert _run(ctx, tool_obj, effect).succeeded is False
    outcome = _run(ctx, tool_obj, effect)

    assert effect.count == 2, "the repair must be attempted again, not replayed"
    assert outcome.succeeded is True
    assert outcome.replayed is False
    assert outcome.attempt == 2
    assert outcome.result == KICKSTART_OK
    assert receipt_state(ctx, _key(ctx), 1) == FAILED
    assert receipt_state(ctx, _key(ctx), 2) == SUCCEEDED
    assert receipt_exists(ctx, _key(ctx))


def test_a_succeeded_receipt_still_dedupes_exactly_as_before(make_ctx):
    """The property the receipt exists for. Unchanged, and it must stay unchanged."""
    effect = Effect(KICKSTART_OK)
    ctx, tool_obj = _ctx(make_ctx, _effect_tool(effect))

    first = _run(ctx, tool_obj, effect)
    second = _run(ctx, tool_obj, effect)
    third = _run(ctx, tool_obj, effect)

    assert effect.count == 1, "no double-kickstart"
    assert first.replayed is False
    assert (second.replayed, third.replayed) == (True, True)
    assert second.result == KICKSTART_OK
    assert second.attempt == 1


def test_a_receipt_written_before_outcomes_existed_still_dedupes(make_ctx, store):
    """Production rows predate the envelope: they must read as DONE, not retryable.

    Reading a legacy row as anything but succeeded would re-run every effect the
    fleet has already performed — a migration that double-sends.
    """
    effect = Effect(KICKSTART_OK)
    ctx, tool_obj = _ctx(make_ctx, _effect_tool(effect))
    key = _key(ctx)
    store.idem_insert(key, ctx.instance_id, "node")
    store.idem_complete(key, json.dumps({"success": True, "legacy": True}))

    outcome = _run(ctx, tool_obj, effect)

    assert effect.count == 0
    assert outcome.replayed is True
    assert outcome.result == {"success": True, "legacy": True}


def test_declared_failure_reads_only_the_conventions_it_documents():
    assert declared_failure(KICKSTART_FAILED)
    assert declared_failure({"ok": False})
    assert declared_failure({"returncode": 2})
    assert declared_failure({"error": "boom"})
    assert not declared_failure(KICKSTART_OK)
    assert not declared_failure({"returncode": 0})
    assert not declared_failure({"escalated": True, "remedy": "unknown"})
    assert not declared_failure("done")
    assert not declared_failure(None)


# ---------------------------------------------------------------------------
# 2. success is not the tool's own say-so
# ---------------------------------------------------------------------------


def _exit_zero_producing_nothing(**kwargs: Any) -> dict[str, Any]:
    """The scraper/GUI failure mode: the command succeeds and does nothing."""
    completed = subprocess.run(  # noqa: S603 - fixed argv, test-local
        ["/bin/sh", "-c", "exit 0"], capture_output=True, text=True, check=False
    )
    return {"returncode": completed.returncode, "stdout": completed.stdout}


def test_exit_zero_with_no_artifact_is_not_a_succeeded_receipt(make_ctx, tmp_path):
    """A tool that exits 0 and produces nothing must not be filed as done."""
    artifact = tmp_path / "render.png"
    tool_obj = _effect_tool(
        _exit_zero_producing_nothing,
        verify=lambda result, args: Verification(
            ok=Path(args["out"]).exists(), detail=f"{args['out']} was not produced"
        ),
    )
    ctx, tool_obj = _ctx(make_ctx, tool_obj)

    outcome = _run(
        ctx,
        tool_obj,
        lambda: _exit_zero_producing_nothing(out=str(artifact)),
        args={"out": str(artifact)},
    )

    assert outcome.succeeded is False, "exit 0 is not evidence the effect happened"
    assert outcome.verified is False
    assert "was not produced" in outcome.detail
    assert receipt_state(ctx, _key(ctx)) == FAILED
    assert not receipt_exists(ctx, _key(ctx))


def test_the_same_tool_with_the_artifact_present_succeeds(make_ctx, tmp_path):
    """Control: the predicate is what decides, and it can say yes."""
    artifact = tmp_path / "render.png"
    artifact.write_text("x", encoding="utf-8")
    tool_obj = _effect_tool(
        _exit_zero_producing_nothing,
        verify=lambda result, args: Path(args["out"]).exists(),
    )
    ctx, tool_obj = _ctx(make_ctx, tool_obj)

    outcome = _run(
        ctx,
        tool_obj,
        lambda: _exit_zero_producing_nothing(out=str(artifact)),
        args={"out": str(artifact)},
    )

    assert outcome.verified is True
    assert receipt_state(ctx, _key(ctx)) == SUCCEEDED


def test_a_verifier_cannot_launder_a_self_declared_failure(make_ctx):
    """Both signals must be non-adverse: verification can only make it harder."""
    effect = Effect(KICKSTART_FAILED)
    ctx, tool_obj = _ctx(make_ctx, _effect_tool(effect, verify=lambda result, args: True))

    assert _run(ctx, tool_obj, effect).succeeded is False
    assert receipt_state(ctx, _key(ctx)) == FAILED


def test_a_verifier_that_answers_nothing_is_not_a_verdict(make_ctx):
    effect = Effect(KICKSTART_OK)
    ctx, tool_obj = _ctx(make_ctx, _effect_tool(effect, verify=lambda result, args: None))

    assert _run(ctx, tool_obj, effect).succeeded is False
    assert receipt_state(ctx, _key(ctx)) == FAILED


def test_a_verifier_that_raises_leaves_the_effect_UNKNOWN(make_ctx):
    """The effect ran; its outcome could not be established. Fail closed."""

    def explode(result: Any, args: Any) -> bool:
        raise RuntimeError("probe timed out")

    effect = Effect(KICKSTART_OK)
    ctx, tool_obj = _ctx(make_ctx, _effect_tool(effect, verify=explode))

    with pytest.raises(EffectStateUnknown):
        _run(ctx, tool_obj, effect)

    assert receipt_state(ctx, _key(ctx)) == "claimed"
    with pytest.raises(EffectStateUnknown):
        _run(ctx, tool_obj, effect)
    assert effect.count == 1, "an unknown outcome is never re-run"


# ---------------------------------------------------------------------------
# 3. the crash windows still behave (nothing above may weaken these)
# ---------------------------------------------------------------------------


def test_claimed_but_unacted_still_fails_closed_on_a_later_attempt(make_ctx, store):
    """The new attempt slots inherit the old guarantee, per row.

    Attempt 1 failed, attempt 2 was claimed and the process died before it acted.
    The next tick must abort — NOT skip to attempt 3, which would be a blind
    re-run of an effect that may already have happened.
    """
    effect = Effect(KICKSTART_FAILED)
    ctx, tool_obj = _ctx(make_ctx, _effect_tool(effect))
    assert _run(ctx, tool_obj, effect).succeeded is False

    store.idem_insert(attempt_key(_key(ctx), 2), ctx.instance_id, "node")  # claim, then "crash"

    with pytest.raises(EffectStateUnknown):
        _run(ctx, tool_obj, effect)
    assert effect.count == 1


def test_a_replay_on_unknown_tool_may_still_retry_its_own_claimed_row(make_ctx, store):
    effect = Effect(KICKSTART_OK)
    ctx, tool_obj = _ctx(make_ctx, _effect_tool(effect, tier=RiskTier.T0, replay_on_unknown=True))
    store.idem_insert(_key(ctx), ctx.instance_id, "node")

    outcome = _run(ctx, tool_obj, effect)

    assert (outcome.replayed, outcome.attempt, effect.count) == (False, 1, 1)
    assert receipt_state(ctx, _key(ctx)) == SUCCEEDED


def test_a_concurrent_claim_still_fails_closed(make_ctx, monkeypatch):
    effect = Effect(KICKSTART_OK)
    ctx, tool_obj = _ctx(make_ctx, _effect_tool(effect))
    monkeypatch.setattr(ctx.store, "idem_insert", lambda *a, **k: False)

    with pytest.raises(EffectStateUnknown, match="concurrently"):
        _run(ctx, tool_obj, effect)
    assert effect.count == 0


# ---------------------------------------------------------------------------
# 4. the retry budget is bounded
# ---------------------------------------------------------------------------


def test_consecutive_failures_park_instead_of_hammering(make_ctx):
    effect = Effect(KICKSTART_FAILED)
    ctx, tool_obj = _ctx(make_ctx, _effect_tool(effect))

    for _ in range(DEFAULT_MAX_ATTEMPTS):
        assert _run(ctx, tool_obj, effect).succeeded is False
    assert effect.count == DEFAULT_MAX_ATTEMPTS

    for _ in range(3):  # every later tick, forever
        with pytest.raises(EffectAttemptsExhausted):
            _run(ctx, tool_obj, effect)
    assert effect.count == DEFAULT_MAX_ATTEMPTS, "an exhausted budget must not reach the tool"


def test_a_t2_effect_gets_exactly_one_attempt_by_default(make_ctx):
    """A tool reporting a failed SEND has not necessarily failed to send."""
    effect = Effect(KICKSTART_FAILED)
    ctx, tool_obj = _ctx(make_ctx, _effect_tool(effect, tier=RiskTier.T2))

    assert _run(ctx, tool_obj, effect).succeeded is False
    with pytest.raises(EffectAttemptsExhausted):
        _run(ctx, tool_obj, effect)
    assert effect.count == 1


def test_a_new_business_key_gets_a_fresh_budget(make_ctx):
    """Bounded per incident, not per loop: a NEW incident may still be repaired."""
    effect = Effect(KICKSTART_FAILED, KICKSTART_FAILED, KICKSTART_FAILED, KICKSTART_OK)
    ctx, tool_obj = _ctx(make_ctx, _effect_tool(effect))
    spent = receipt_key(ctx.instance_id, ctx.template, "node", "effect", "incident-1")
    fresh = receipt_key(ctx.instance_id, ctx.template, "node", "effect", "incident-2")

    for _ in range(DEFAULT_MAX_ATTEMPTS):
        assert (
            guarded(ctx, key=spent, node="node", tool=tool_obj, execute=effect).succeeded is False
        )
    with pytest.raises(EffectAttemptsExhausted):
        guarded(ctx, key=spent, node="node", tool=tool_obj, execute=effect)

    outcome = guarded(ctx, key=fresh, node="node", tool=tool_obj, execute=effect)
    assert outcome.result == KICKSTART_OK


def test_an_unbounded_retry_budget_cannot_be_declared():
    for bad in (0, -1, MAX_ATTEMPTS_CEILING + 1, 1000):
        with pytest.raises(ValueError, match="max_attempts"):
            _effect_tool(Effect(KICKSTART_OK), max_attempts=bad)


# ---------------------------------------------------------------------------
# 5. end to end through the real template — the live W3 shape
# ---------------------------------------------------------------------------


def _monitor_ctx(make_ctx: Any, repair_tool: LoopTool) -> Any:
    registry = ToolRegistry()
    registry.register(tool("monitor", RiskTier.T0, Counter(result={"api": "down"})))
    registry.register(
        tool("diagnose", RiskTier.T0, Counter(result={"remedy": "restart_api", "incident": "i-1"}))
    )
    registry.register(repair_tool)
    registry.register(tool("escalate", RiskTier.T3, Counter()))
    registry.register(tool("verify", RiskTier.T0, Counter(result={"ok": True})))
    return make_ctx(
        instance_id="error_monitor",
        template="monitor_diagnose_repair_verify",
        params={"allowed_remedies": ["restart_api"]},
        tools=registry,
    )


def _repair_tool(call: Any, **kwargs: Any) -> LoopTool:
    return LoopTool(
        name="repair",
        tier=RiskTier.T1,
        idempotency_key=lambda args: "restart_api:i-1",
        call=call,
        description="launchctl kickstart",
        **kwargs,
    )


def _repair_receipt(ctx: Any) -> str:
    return receipt_key(ctx.instance_id, ctx.template, "repair", "repair", "restart_api:i-1")


def test_a_failed_repair_is_retried_on_the_next_tick_of_the_same_incident(make_ctx):
    """The live incident, end to end, through the real template and runtime.

    Asserted on the RECEIPT and the number of launchctl calls, not on the tick's
    status: what the tick renders is the verify node's call (P1), and this lane
    must not be the thing that decides it.
    """
    template = get_template("monitor_diagnose_repair_verify")
    repair = Effect(KICKSTART_FAILED, KICKSTART_OK)
    ctx = _monitor_ctx(make_ctx, _repair_tool(repair))
    key = _repair_receipt(ctx)

    run_once(ctx, template)
    assert repair.count == 1
    assert receipt_state(ctx, key, 1) == FAILED
    assert not receipt_exists(ctx, key), "a failed repair is not a handled incident"

    run_once(ctx, template)
    assert repair.count == 2, "the incident must be re-attempted, not filed as handled"
    assert receipt_state(ctx, key, 2) == SUCCEEDED

    run_once(ctx, template)
    assert repair.count == 2, "and once repaired, the receipt dedupes as before"


def test_a_repair_that_never_works_escalates_and_stops_calling_launchctl(make_ctx):
    template = get_template("monitor_diagnose_repair_verify")
    repair = Effect(KICKSTART_FAILED)
    ctx = _monitor_ctx(make_ctx, _repair_tool(repair))

    for _ in range(DEFAULT_MAX_ATTEMPTS):
        run_once(ctx, template)
    assert repair.count == DEFAULT_MAX_ATTEMPTS

    for _ in range(2):  # every tick after the budget, forever
        report = run_once(ctx, template)
        assert report.status is LoopStatus.ABORTED
        assert "EffectAttemptsExhausted" in report.detail
    assert repair.count == DEFAULT_MAX_ATTEMPTS, "an exhausted incident must not hammer launchctl"


def test_the_template_threads_the_effect_arguments_into_the_verifier(make_ctx):
    """A verification predicate reads the ARGS the tool was called with."""
    template = get_template("monitor_diagnose_repair_verify")
    seen: list[dict[str, Any]] = []

    def verify(result: Any, args: Any) -> Verification:
        seen.append(dict(args))
        return Verification(ok=False, detail="the service is still not running")

    repair = Effect(KICKSTART_OK)
    ctx = _monitor_ctx(make_ctx, _repair_tool(repair, verify=verify))

    run_once(ctx, template)

    # Deliberately not asserting the whole args mapping: which keys the template
    # hands its repair tool is the template's contract (the w3 lane is changing
    # it from ``snapshot`` to ``diagnosis``), while THREADING them to the
    # verifier at all is this lane's.
    assert seen and seen[0]["remedy"] == "restart_api"
    assert repair.count == 1
    assert receipt_state(ctx, _repair_receipt(ctx)) == FAILED, (
        "launchctl exited 0 but the probe says the service is down — not a success"
    )
