"""Idempotency receipts: what may replay, what must not, and what is unknowable.

Every test here runs against both shipped storage backends, because a receipt is
only worth what the store underneath it guarantees — and the two stores arbitrate
the claim race by genuinely different means (a ``threading.Lock`` and a primary
key).

The five row states and what the next tick does with them are the whole subject:

======================  ==========================================
row                     the next tick
======================  ==========================================
absent                  claim it and act
claimed, no result      FAIL CLOSED forever (``EffectStateUnknown``)
``succeeded``           replay the recorded result
``failed``              attempt the NEXT slot, spending budget
``unavailable``         attempt the NEXT slot, spending NO budget
======================  ==========================================

The last row is FIX-9 and it is the one with a history. The design this was
ported from RELEASED the claim on an unreachable dependency, so that an outage
would not spend the business key's retry budget. But releasing is a *second*
store call, and a process that died between the claim and the release left the
row claimed-with-no-result — which bricks the business key with exactly the
``EffectStateUnknown`` the release existed to avoid. The release existed to make
an outage harmless and it opened a window in which an outage was permanently
harmful. Recording a terminal ``unavailable`` outcome is one durable write with
no window, and
:func:`test_an_unavailable_row_survives_a_crash_between_the_two_writes` is the
test that says so.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

import pytest
from selfloop.context import LoopContext
from selfloop.contracts import (
    MAX_ATTEMPTS_CEILING,
    EffectAttemptsExhausted,
    EffectDenied,
    EffectStateUnknown,
    EffectUnavailable,
    LoopError,
    LoopTool,
    RecordKind,
    RiskTier,
    Verification,
)
from selfloop.ledger import (
    RECEIPT_FAILED,
    RECEIPT_SUCCEEDED,
    RECEIPT_UNAVAILABLE,
)
from selfloop.receipts import (
    CLAIMED,
    ReceiptOutcome,
    attempt_key,
    declared_failure,
    guarded,
    receipt_exists,
    receipt_key,
    receipt_state,
    reconcile,
)

NODE = "send"


class Executions:
    """A scripted effect that counts how many times it was actually reached.

    "The tool was not reached" is an assertion this suite makes constantly —
    exhaustion, a short-circuited replay, a claimed row — and it is only
    meaningful if something counts. A bare lambda cannot.
    """

    def __init__(self, *results: Any, raises: BaseException | None = None) -> None:
        self.calls = 0
        self._results = list(results)
        self._raises = raises

    def __call__(self) -> Any:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        if not self._results:
            return {"success": True, "id": f"call-{self.calls}"}
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]


def key_for(ctx: LoopContext, business_key: str = "msg-42", node: str = NODE) -> str:
    return receipt_key(ctx.instance_id, ctx.template, node, "send_reply", business_key)


def run(
    ctx: LoopContext,
    tool: LoopTool,
    execute: Callable[[], Any],
    *,
    business_key: str = "msg-42",
    node: str = NODE,
) -> ReceiptOutcome:
    """One guarded attempt at *business_key*, as a node would make it."""
    return guarded(
        ctx,
        key=key_for(ctx, business_key, node),
        node=node,
        tool=tool,
        execute=execute,
        args={"to": "someone@example.com"},
        business_key=business_key,
    )


@pytest.fixture
def tool(make_tool: Callable[..., LoopTool]) -> LoopTool:
    """A reversible tool with a two-attempt budget. Not sealed; see ``conftest``."""
    return make_tool(name="send_reply", tier=RiskTier.T1, max_attempts=2)


# ---------------------------------------------------------------------------
# Key composition: what makes two effects two effects
# ---------------------------------------------------------------------------


def test_the_key_is_injective_in_every_part(ctx: LoopContext) -> None:
    """Joined with an ordinary character, these two would be ONE key.

    Node ``send`` with business key ``a:b`` and node ``send:a`` with business key
    ``b`` compose the same string under a ``:`` separator — and whichever is
    written first then suppresses the other's effect forever, which reads exactly
    like a loop with nothing to do.
    """
    left = receipt_key("t1", "demo", "send", "tool", "a:b")
    right = receipt_key("t1", "demo", "send:a", "tool", "b")
    assert left != right


def test_a_structural_part_may_not_contain_the_separator() -> None:
    with pytest.raises(ValueError, match="boundaries are ambiguous"):
        receipt_key("t1", "demo", "se\x1fnd", "tool", "msg-1")


def test_a_business_key_may_not_impersonate_an_attempt_row() -> None:
    """Otherwise attempt 2 of one effect is attempt 1 of another."""
    with pytest.raises(ValueError, match="attempt suffix"):
        receipt_key("t1", "demo", "send", "tool", "msg-1#a2")


def test_the_first_attempt_names_the_effect_and_not_a_slot() -> None:
    assert attempt_key("k", 1) == "k"
    assert attempt_key("k", 2) == "k#a2"
    with pytest.raises(ValueError):
        attempt_key("k", 0)


# ---------------------------------------------------------------------------
# Budgets: fresh for a new business key, spent for a retried one
# ---------------------------------------------------------------------------


def test_a_new_business_key_gets_a_fresh_budget(ctx: LoopContext, tool: LoopTool) -> None:
    """The budget belongs to the effect's IDENTITY, not to the tool or the tick."""
    failing = Executions({"success": False, "error": "smtp said no"})

    assert run(ctx, tool, failing, business_key="msg-42").succeeded is False
    assert run(ctx, tool, failing, business_key="msg-42").succeeded is False
    with pytest.raises(EffectAttemptsExhausted):
        run(ctx, tool, failing, business_key="msg-42")
    assert failing.calls == 2

    # A different message is a different effect, and starts from a full budget.
    fresh = Executions()
    outcome = run(ctx, tool, fresh, business_key="msg-43")
    assert outcome.succeeded is True
    assert outcome.attempt == 1
    assert fresh.calls == 1


def test_a_retried_business_key_does_not_get_a_fresh_budget(
    ctx: LoopContext, tool: LoopTool
) -> None:
    """Two ticks that MEAN the same effect must produce the same key.

    The retry budget is structural — how many rows exist — rather than a counter
    somebody has to keep consistent across a crash.
    """
    failing = Executions({"success": False})
    for expected_attempt in (1, 2):
        assert run(ctx, tool, failing).attempt == expected_attempt
    with pytest.raises(EffectAttemptsExhausted):
        run(ctx, tool, failing)


def test_a_failed_row_frees_the_next_slot(ctx: LoopContext, tool: LoopTool) -> None:
    """A recorded failure is not a completed effect, and must not suppress a retry.

    The defect this closes: a repair tool returned ``{"success": false}``, the
    guard completed the receipt with whatever the tool returned, and the *failed*
    repair was filed as a done effect. Because the business key was stable for
    the whole incident window, that receipt suppressed every subsequent retry.
    The service stayed down; the loop reported the incident handled.
    """
    key = key_for(ctx)
    first = run(ctx, tool, Executions({"success": False, "error": "smtp said no"}))
    assert first.succeeded is False
    assert first.attempt == 1
    assert first.key == key
    assert receipt_state(ctx, key, 1) == RECEIPT_FAILED

    second = run(ctx, tool, Executions({"success": True, "id": "m-1"}))
    assert second.succeeded is True
    assert second.attempt == 2
    assert second.key == attempt_key(key, 2)
    assert receipt_state(ctx, key, 2) == RECEIPT_SUCCEEDED


def test_a_failed_attempt_returns_rather_than_raising(
    ctx: LoopContext, tool: LoopTool
) -> None:
    """The tick's STATUS is the template's decision, not this seam's.

    A seam that raised would skip the very node whose job is to explain, in its
    own domain's terms, what "it did not work" means. What this module owes a
    caller is an honest answer about the world, not a verdict about the tick.
    """
    outcome = run(ctx, tool, Executions({"success": False, "error": "smtp said no"}))
    assert outcome.succeeded is False
    assert outcome.replayed is False
    assert "1 attempt(s) left" in outcome.detail
    assert outcome.result == {"success": False, "error": "smtp said no"}


def test_exhaustion_raises_without_reaching_the_tool(
    ctx: LoopContext, tool: LoopTool
) -> None:
    """A permanently-failing effect escalates; it never hammers a system per tick."""
    failing = Executions({"success": False})
    run(ctx, tool, failing)
    run(ctx, tool, failing)
    assert failing.calls == 2

    with pytest.raises(EffectAttemptsExhausted) as caught:
        run(ctx, tool, failing)

    assert failing.calls == 2, "the tool must not be reached once the budget is spent"
    assert "escalating to a human" in str(caught.value)
    # A subclass of EffectDenied, so every template's existing denial handler
    # parks on it — which is the intended behaviour, not a coincidence.
    assert isinstance(caught.value, EffectDenied)


def test_the_approval_floor_tier_gets_exactly_one_attempt(
    ctx: LoopContext, make_tool: Callable[..., LoopTool]
) -> None:
    """A tool that reports failure on an outbound send has not necessarily failed
    to send it, so T2+ is allowed one attempt and then a human."""
    outbound = make_tool(name="send_reply", tier=RiskTier.T2)
    assert outbound.resolved_max_attempts() == 1

    failing = Executions({"success": False})
    assert run(ctx, outbound, failing).succeeded is False
    with pytest.raises(EffectAttemptsExhausted):
        run(ctx, outbound, failing)
    assert failing.calls == 1


# ---------------------------------------------------------------------------
# Success, and what it takes to claim it
# ---------------------------------------------------------------------------


def test_a_succeeded_row_short_circuits_the_replay(
    ctx: LoopContext, tool: LoopTool
) -> None:
    """One merge, replayed, produced two merges. The guarded variant produced one."""
    execute = Executions({"success": True, "id": "m-1"})
    first = run(ctx, tool, execute)
    second = run(ctx, tool, execute)

    assert execute.calls == 1
    assert first.replayed is False
    assert second.replayed is True
    assert second.result == {"success": True, "id": "m-1"}
    assert second.key == first.key
    assert receipt_exists(ctx, key_for(ctx)) is True


def test_a_verifier_can_only_ever_make_success_harder(
    ctx: LoopContext, make_tool: Callable[..., LoopTool]
) -> None:
    """The conjunction is monotone, so a weak verifier cannot launder a failure.

    Here the tool declares success and the independent check says the message is
    not in the sent folder. The receipt records ``failed``.
    """
    unverified = make_tool(
        name="send_reply",
        tier=RiskTier.T1,
        max_attempts=2,
        verify=lambda _result, _args: Verification(ok=False, detail="not in the sent folder"),
    )
    outcome = run(ctx, unverified, Executions({"success": True, "id": "m-1"}))

    assert outcome.succeeded is False
    assert outcome.verified is False
    assert "not in the sent folder" in outcome.detail
    assert receipt_state(ctx, key_for(ctx), 1) == RECEIPT_FAILED


def test_a_declared_verifier_is_recorded_on_the_mirrored_row(
    ctx: LoopContext, make_tool: Callable[..., LoopTool]
) -> None:
    """``verified is None`` means "no independent check ran" and never "fine".

    The learning pass mines exactly the disagreement between what a tool declared
    and what a verifier ruled, and it cannot do that if absence has been
    laundered into agreement.
    """
    silent = make_tool(name="send_reply", tier=RiskTier.T1)
    assert run(ctx, silent, Executions({"success": True})).verified is None
    unmirrored = ctx.records.get(RecordKind.RECEIPT.value, key_for(ctx))
    assert unmirrored is not None
    assert unmirrored["verified"] is None
    assert unmirrored["declared_success"] is True
    assert unmirrored["outcome"] == RECEIPT_SUCCEEDED
    assert unmirrored["evidence_grade"] == 0  # ACTOR_NARRATIVE: the tool's own opinion


@pytest.mark.parametrize(
    ("result", "admits"),
    [
        ({}, False),
        ({"success": True}, False),
        ({"success": False}, True),
        ({"ok": False}, True),
        ({"returncode": 0}, False),
        ({"returncode": 1}, True),
        ({"error": "boom"}, True),
        ({"ok": True, "error": "a warning, not a failure"}, False),
        ("a plain string", False),
        (None, False),
    ],
)
def test_declared_failure_only_ever_vetoes(result: Any, admits: bool) -> None:
    """Read it as "did the tool admit to failing?", never as "did it succeed?"."""
    assert bool(declared_failure(result)) is admits


# ---------------------------------------------------------------------------
# Unknown: the state that must never resolve itself
# ---------------------------------------------------------------------------


def test_a_claimed_but_uncompleted_row_fails_closed_forever(
    ctx: LoopContext, tool: LoopTool
) -> None:
    """A crash between claim and completion. The effect MAY have happened.

    It fails on every tick until a human reconciles it, and the message says so,
    because a loud failure that does not name its recovery path is only half
    loud. A TTL here would be the double-billing bug with a delay in front of
    it — a timer observes nothing.
    """
    key = key_for(ctx)
    assert ctx.receipts.claim(key, instance_id=ctx.instance_id, node=NODE, at="") is True
    assert receipt_state(ctx, key) == CLAIMED

    execute = Executions()
    for _ in range(4):
        with pytest.raises(EffectStateUnknown) as caught:
            run(ctx, tool, execute)
        assert "will not be re-run" in str(caught.value)
        assert ctx.reconcile_hint in str(caught.value)

    assert execute.calls == 0
    assert receipt_exists(ctx, key) is False


def test_a_verify_predicate_that_raises_becomes_unknown_and_not_failed(
    ctx: LoopContext, make_tool: Callable[..., LoopTool]
) -> None:
    """The effect RAN and its outcome could not be established. That is not a failure.

    Treating the raise as "it failed" would free the effect to run again — so a
    verifier whose own dependency is down would cause the double-billing the
    whole layer exists to prevent.
    """

    def verify_raises(_result: Any, _args: Mapping[str, Any]) -> bool:
        raise ConnectionError("the sent-folder API is down")

    tool = make_tool(name="send_reply", tier=RiskTier.T1, max_attempts=2, verify=verify_raises)
    execute = Executions({"success": True, "id": "m-1"})

    with pytest.raises(EffectStateUnknown, match="verification predicate raised"):
        run(ctx, tool, execute)

    key = key_for(ctx)
    assert execute.calls == 1
    assert receipt_state(ctx, key, 1) == CLAIMED  # NOT failed, and NOT succeeded
    assert ctx.records.get(RecordKind.RECEIPT.value, key) is None

    # And it stays unknown: the next tick does not re-send and does not spend a slot.
    with pytest.raises(EffectStateUnknown):
        run(ctx, tool, execute)
    assert execute.calls == 1
    assert receipt_state(ctx, key, 2) == "absent"


def test_a_payload_this_package_did_not_write_is_unknown_and_not_success(
    ctx: LoopContext, tool: LoopTool
) -> None:
    """"Somebody completed it, so it is done" is how a corrupt row becomes a success."""
    key = key_for(ctx)
    ctx.receipts.claim(key, instance_id=ctx.instance_id, node=NODE, at="")
    ctx.receipts.complete(key, envelope_json='{"status": "done"}', at="")

    execute = Executions()
    with pytest.raises(EffectStateUnknown, match="payload this package cannot read"):
        run(ctx, tool, execute)
    assert execute.calls == 0


def test_replay_on_unknown_is_the_documented_opt_out(
    ctx: LoopContext, make_tool: Callable[..., LoopTool]
) -> None:
    """"Re-running this is harmless" is a claim a tool makes explicitly, at T0/T1."""
    replayable = make_tool(name="send_reply", tier=RiskTier.T1, replay_on_unknown=True)
    key = key_for(ctx)
    ctx.receipts.claim(key, instance_id=ctx.instance_id, node=NODE, at="")

    execute = Executions({"success": True})
    outcome = run(ctx, replayable, execute)

    assert execute.calls == 1
    assert outcome.succeeded is True
    assert receipt_state(ctx, key, 1) == RECEIPT_SUCCEEDED


def test_replay_on_unknown_is_forbidden_above_the_approval_floor() -> None:
    """At T2+, "unknown" means a human-visible act may already have happened."""
    with pytest.raises(ValueError, match="forbidden at tier T2"):
        LoopTool(name="send", tier=RiskTier.T2, call=lambda: None, replay_on_unknown=True)


# ---------------------------------------------------------------------------
# reconcile: the audited way out
# ---------------------------------------------------------------------------


def test_reconcile_settles_an_unknown_and_the_next_tick_honours_it(
    ctx: LoopContext, tool: LoopTool
) -> None:
    key = key_for(ctx)
    ctx.receipts.claim(key, instance_id=ctx.instance_id, node=NODE, at="")

    record = reconcile(ctx, key, outcome=RECEIPT_SUCCEEDED, by="owner", note="found in the log")

    assert record.outcome == RECEIPT_SUCCEEDED
    assert receipt_state(ctx, key) == RECEIPT_SUCCEEDED
    assert ctx.records.get(RecordKind.RECONCILIATION.value, record.id) is not None

    execute = Executions()
    assert run(ctx, tool, execute).replayed is True
    assert execute.calls == 0


def test_reconciling_a_failure_frees_the_next_slot(
    ctx: LoopContext, tool: LoopTool
) -> None:
    key = key_for(ctx)
    ctx.receipts.claim(key, instance_id=ctx.instance_id, node=NODE, at="")
    reconcile(ctx, key, outcome=RECEIPT_FAILED, by="owner")

    execute = Executions({"success": True})
    outcome = run(ctx, tool, execute)
    assert outcome.attempt == 2
    assert execute.calls == 1


def test_a_loop_cannot_clear_its_own_unknowns(ctx: LoopContext) -> None:
    """Structural rather than forbidden: ``loop:<instance>`` is the only identity
    the loop can write, and the record refuses that prefix."""
    key = key_for(ctx)
    ctx.receipts.claim(key, instance_id=ctx.instance_id, node=NODE, at="")

    with pytest.raises(ValueError, match="automation identity"):
        reconcile(ctx, key, outcome=RECEIPT_SUCCEEDED, by=ctx.actor)
    assert receipt_state(ctx, key) == CLAIMED


def test_an_undecided_reconciliation_is_refused(ctx: LoopContext) -> None:
    """"Probably fine" is the state we are already in."""
    key = key_for(ctx)
    ctx.receipts.claim(key, instance_id=ctx.instance_id, node=NODE, at="")
    with pytest.raises(ValueError, match="must be decisive"):
        reconcile(ctx, key, outcome="probably", by="owner")


def test_reconcile_refuses_an_absent_row_and_a_settled_one(
    ctx: LoopContext, tool: LoopTool
) -> None:
    with pytest.raises(LoopError, match="no such row"):
        reconcile(ctx, key_for(ctx), outcome=RECEIPT_SUCCEEDED, by="owner")

    run(ctx, tool, Executions({"success": True}))
    with pytest.raises(LoopError, match="already records"):
        reconcile(ctx, key_for(ctx), outcome=RECEIPT_FAILED, by="owner")


# ---------------------------------------------------------------------------
# FIX-9: unavailable — the effect's authority was never reached
# ---------------------------------------------------------------------------

#: Only a side that can PROVE no request left it may raise this: no socket,
#: connection refused, DNS failure, a credential missing before the first
#: request byte. A server that ANSWERED is a refusal, which is adverse.
UNREACHABLE = EffectUnavailable("connection_refused", "no socket at 127.0.0.1:9")


def test_an_unavailable_effect_records_a_terminal_envelope(
    ctx: LoopContext, tool: LoopTool
) -> None:
    """One durable write, and the row is terminal rather than deleted."""
    execute = Executions(raises=UNREACHABLE)
    with pytest.raises(EffectUnavailable) as caught:
        run(ctx, tool, execute)

    key = key_for(ctx)
    assert caught.value.reason == "connection_refused"
    assert execute.calls == 1
    assert receipt_state(ctx, key, 1) == RECEIPT_UNAVAILABLE

    mirrored = ctx.records.get(RecordKind.RECEIPT.value, key)
    assert mirrored is not None
    assert mirrored["outcome"] == RECEIPT_UNAVAILABLE
    assert mirrored["declared_success"] is None
    assert mirrored["verified"] is None

    events = [e for e in ctx.events.read(after=0, limit=100) if e["action"] == "effect_unavailable"]
    assert events and events[-1]["payload"]["outcome_recorded"] is True


def test_an_unavailable_row_frees_the_next_slot_without_spending_budget(
    ctx: LoopContext, make_tool: Callable[..., LoopTool]
) -> None:
    """An outage costs a ROW, not a RETRY.

    The tool below is allowed exactly one attempt, so if an absence spent budget
    the second call would raise ``EffectAttemptsExhausted`` — manufacturing a
    failure verdict out of a proof that nothing happened.
    """
    one_shot = make_tool(name="send_reply", tier=RiskTier.T1, max_attempts=1)

    with pytest.raises(EffectUnavailable):
        run(ctx, one_shot, Executions(raises=UNREACHABLE))

    execute = Executions({"success": True, "id": "m-1"})
    outcome = run(ctx, one_shot, execute)

    assert outcome.succeeded is True
    assert outcome.attempt == 2
    assert execute.calls == 1
    assert receipt_state(ctx, key_for(ctx), 1) == RECEIPT_UNAVAILABLE


class _Sigkill(BaseException):
    """A process death, simulated. Not an ``Exception``, so no handler catches it."""


class CrashingRecords:
    """A record store whose first ``put_latest`` kills the process.

    Deliberately a ``BaseException``: :func:`selfloop.receipts._mirror` catches
    ``Exception`` and folds a mirror failure into the event log, which is correct
    — the effect has already happened and its authoritative record is already
    durable. What that handler cannot survive, and must not need to, is the
    machine going away, which is what this models.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.armed = True

    def put_once(self, kind: str, record_id: str, payload: Mapping[str, Any]) -> bool:
        return bool(self._inner.put_once(kind, record_id, payload))

    def put_latest(self, kind: str, record_id: str, payload: Mapping[str, Any]) -> None:
        if self.armed:
            self.armed = False
            raise _Sigkill("the process died after the receipt was completed")
        self._inner.put_latest(kind, record_id, payload)

    def get(self, kind: str, record_id: str) -> Mapping[str, Any] | None:
        return self._inner.get(kind, record_id)

    def query(self, kind: str, /, **equals: Any) -> list[Mapping[str, Any]]:
        return list(self._inner.query(kind, **equals))

    def transition(
        self, kind: str, record_id: str, *, expect: Mapping[str, Any], set: Mapping[str, Any]
    ) -> bool:
        return bool(self._inner.transition(kind, record_id, expect=expect, set=set))


class RefusingReceipts:
    """A receipt store that cannot complete a row. The write that must not be lost."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def claim(self, key: str, *, instance_id: str, node: str, at: str) -> bool:
        return bool(self._inner.claim(key, instance_id=instance_id, node=node, at=at))

    def get(self, key: str) -> Mapping[str, Any] | None:
        return self._inner.get(key)

    def complete(self, key: str, *, envelope_json: str, at: str) -> None:
        raise OSError("the database volume went away")

    def release(self, key: str) -> bool:
        return bool(self._inner.release(key))


def test_an_unavailable_row_survives_a_crash_between_the_two_writes(
    make_ctx: Callable[..., Any], tool: LoopTool
) -> None:
    """THE property FIX-9 bought: the terminal write lands before anything else can fail.

    The process dies immediately after the receipt row is completed and before
    its readable mirror is written. A restarted process must still see a terminal
    ``unavailable`` row — so the next attempt slot is free, no retry budget was
    spent, and the business key is emphatically not bricked.
    """
    live = make_ctx()
    dying = replace(live, records=CrashingRecords(live.records))

    with pytest.raises(_Sigkill):
        run(dying, tool, Executions(raises=UNREACHABLE))

    # A new process: new port objects over the same durable storage.
    restarted = make_ctx()
    key = key_for(restarted)
    assert receipt_state(restarted, key, 1) == RECEIPT_UNAVAILABLE

    execute = Executions({"success": True, "id": "m-1"})
    outcome = run(restarted, tool, execute)

    assert outcome.succeeded is True
    assert outcome.attempt == 2
    assert execute.calls == 1


def test_a_crash_before_the_unavailable_write_fails_closed_and_says_so(
    make_ctx: Callable[..., Any], tool: LoopTool
) -> None:
    """The honest outcome when the ONE durable write is the thing that failed.

    Nothing left this process, but this process also failed to write down that
    nothing left it — so the next tick fails closed and asks a human. It is
    recorded loudly rather than swallowed, and the original
    ``EffectUnavailable`` still propagates because it is the real answer about
    the world.
    """
    live = make_ctx()
    refusing = replace(live, receipts=RefusingReceipts(live.receipts))

    with pytest.raises(EffectUnavailable) as caught:
        run(refusing, tool, Executions(raises=UNREACHABLE))
    assert caught.value.reason == "connection_refused"

    events = [e for e in live.events.read(after=0, limit=100) if e["action"] == "effect_unavailable"]
    assert events and events[-1]["payload"]["outcome_recorded"] is False

    key = key_for(live)
    assert receipt_state(live, key, 1) == CLAIMED
    with pytest.raises(EffectStateUnknown):
        run(live, tool, Executions())


def test_a_wall_of_absences_reports_unavailability_rather_than_inventing_a_failure(
    ctx: LoopContext, make_tool: Callable[..., LoopTool]
) -> None:
    """Ten proofs that nothing happened do not add up to one failure.

    Two ceilings that count different things: ``max_attempts`` bounds recorded
    FAILURES, and ``MAX_ATTEMPTS_CEILING`` bounds ROWS — because an unavailable
    slot spends no budget and something has to stop a month-long outage writing
    one row per tick forever. When the rows run out and none of them is a
    failure, the answer is still absence.
    """
    one_shot = make_tool(name="send_reply", tier=RiskTier.T1, max_attempts=1)
    execute = Executions(raises=UNREACHABLE)

    for _ in range(MAX_ATTEMPTS_CEILING):
        with pytest.raises(EffectUnavailable) as caught:
            run(ctx, one_shot, execute)
        assert caught.value.reason == "connection_refused"
    assert execute.calls == MAX_ATTEMPTS_CEILING

    with pytest.raises(EffectUnavailable) as caught:
        run(ctx, one_shot, execute)

    assert caught.value.reason == "attempt_slots_exhausted"
    assert execute.calls == MAX_ATTEMPTS_CEILING, "the tool must not be reached"
    assert not isinstance(caught.value, EffectAttemptsExhausted)


def test_receipt_exists_means_a_SUCCEEDED_row_and_nothing_else(
    ctx: LoopContext, tool: LoopTool
) -> None:
    """"Any row means done" is what suppressed a whole incident window of retries."""
    key = key_for(ctx)
    assert receipt_exists(ctx, key) is False

    run(ctx, tool, Executions({"success": False}))
    assert receipt_state(ctx, key, 1) == RECEIPT_FAILED
    assert receipt_exists(ctx, key) is False

    run(ctx, tool, Executions({"success": True}))
    assert receipt_exists(ctx, key) is True
