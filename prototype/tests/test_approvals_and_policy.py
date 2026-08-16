"""The park/approve contract: what a human's decision does and does not authorise.

Every test here defends one sentence from ``selfloop.policy`` or
``selfloop.approvals``, and each of those sentences was bought by a failure that
actually happened in the system this package was extracted from. The suite is
organised around the five that cost the most:

1. **The tier floor is not negotiable.** A caller's ``PolicyPort`` is consulted
   and then overruled upward. There is no value an adapter can return that lets a
   T2 effect execute unattended, and ``test_tier_floor_*`` pins that by handing
   the gate the most permissive adapter that can be written.
2. **An approval is for an ACTION, not a slot.** The argument digest is inside
   both the row's binding and the id's preimage. Change one argument and the
   human's decision stops matching — and, because the id moved too, the effect
   parks again on a NEW row instead of deadlocking against the old one forever.
   That second half is FIX-10 and it is the difference between "fails closed" and
   "bricked".
3. **Absence is never authority.** A missing row, an unparseable deadline, a
   state this package does not recognise, a decision made by an automation
   identity: every one of them reads as *not approved*. There is no path through
   :func:`~selfloop.approvals.read_outcome` that turns an absence into a
   permission, and several tests below exist only to try to find one.
4. **Expiry binds over an explicit approval.** A row that reads ``approved`` past
   its deadline is not resume authority. A human authorised an act at a moment,
   not a standing permission.
5. **The approvals page is not a place credentials are published**, and redacting
   it must not change what the human's approval is bound to. The digest is
   computed from the real arguments; only the human-readable copy is redacted.

The suite deliberately reaches through the execution seam for the invalidation
tests rather than asserting on ``read_outcome`` alone. The seam is where the
guarantee has to hold — a template can lose its gate to a refactor, and the
promise is that it still executes zero unauthorised effects — so the assertions
that matter most are phrased as "the tool's call counter did not move".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from selfloop.adapters.memory import RecordingNotifier, build_memory_context
from selfloop.approvals import (
    AUTOMATION_IDENTITY_PREFIXES,
    DEFAULT_TTL_HOURS,
    approval_id,
    deep_link,
    ensure_approval,
    page,
    read_outcome,
    redact_args,
    resolve_for_resume,
    stored_binding,
)
from selfloop.context import LoopContext
from selfloop.contracts import (
    APPROVAL_FLOOR_TIER,
    ActionClass,
    ApprovalState,
    EffectDenied,
    EffectNotApproved,
    GateVerdict,
    LoopError,
    LoopTool,
    PolicyDecision,
    PolicyError,
    RecordKind,
    RiskTier,
    args_digest,
)
from selfloop.policy import TierPolicy, evaluate_tool, preview
from selfloop.tools import effect_binding, execute_effect

PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]

HUMAN = "ops@example.com"
NODE = "send"
BUSINESS_KEY = "message-42"


# ---------------------------------------------------------------------------
# Doubles. Each one exists to be WORSE than anything shipped, because a guard is
# only proven by the most permissive adapter somebody could plausibly write.
# ---------------------------------------------------------------------------


class PermissivePolicy:
    """A ``PolicyPort`` that approves of everything. The adversary for the floor.

    This is not a straw man. The system this package was extracted from shipped a
    policy that returned ``requires_approval=False`` for CONSEQUENTIAL in its
    default (interactive) mode, which is defensible when a person is watching the
    terminal and catastrophic when the same policy is inherited by an unattended
    worker. The floor exists because the loop does not inherit the person.
    """

    approval_expiry_hours = 8

    def __init__(self) -> None:
        self.seen: list[ActionClass] = []

    def evaluate(self, action_class: ActionClass) -> PolicyDecision:
        self.seen.append(action_class)
        return PolicyDecision(
            requires_approval=False,
            reason="this adapter approves of everything",
            action_class=action_class,
        )


class RaisingPolicy:
    """A ``PolicyPort`` that cannot classify anything, in one of two ways."""

    approval_expiry_hours = 8

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    def evaluate(self, action_class: ActionClass) -> PolicyDecision:
        self.calls += 1
        raise self._exc


class BrokenClock:
    """A clock whose record stamp is not a stamp. See :func:`_expires_at`."""

    def __init__(self, stamp: str = "shortly") -> None:
        self.stamp = stamp

    def now_iso(self) -> str:
        return self.stamp

    def elapsed(self) -> float:
        return 0.0


class ExplodingNotifier:
    """A notifier that raises. Paging must never be able to fail a tick."""

    def __init__(self) -> None:
        self.attempts = 0

    def page(self, *, approval_id: str, summary: str, deep_link: str) -> bool:
        self.attempts += 1
        raise RuntimeError("the webhook host is unreachable")


class Counted:
    """A tool implementation that records how many times it was actually reached.

    Every "refused" assertion in this file is phrased against ``calls`` rather
    than against an exception type alone, because the promise the package makes
    is about the external world and not about the shape of a traceback.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {"ok": True, "delivered_to": kwargs.get("to")}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def ctx(
    make_ctx: Callable[..., LoopContext], notifier: RecordingNotifier
) -> LoopContext:
    """A context on the SHIPPED defaults, over the parametrised storage backend.

    Built from ``conftest``'s ``make_ctx`` rather than from
    ``build_memory_context`` directly, so every approval test in this file runs
    TWICE — once against dicts and once against sqlite. Approvals are the most
    storage-sensitive surface in the package: the binding re-check compares a
    live mapping against one that has been through a JSON round trip, and
    ``decide`` is a compare-and-set that is a lock in one adapter and a
    conditional UPDATE in the other. A suite that only ever ran against dicts
    would let a binding that matches in Python and not in JSON ship green.

    The policy is pinned to the shipped :class:`~selfloop.policy.TierPolicy` and
    the notifier to a recording one; nothing else about the context is special,
    and there are no demo-only knobs anywhere in this file.
    """
    return make_ctx(
        instance_id="inst-1",
        template="tpl-1",
        policy=TierPolicy(),
        notifier=notifier,
        dashboard_origin="https://ops.example.com",
    )


def _register(ctx: LoopContext, name: str, tier: RiskTier, **kw: Any) -> LoopTool:
    """Grant a tool to *ctx* and return the SEALED record the registry kept."""
    kw.setdefault("description", f"{name} something")
    return ctx.tools.register(LoopTool(name=name, tier=tier, call=Counted(), **kw))


def _park_verdict(tool: LoopTool, ctx: LoopContext, args: Mapping[str, Any]) -> GateVerdict:
    verdict = evaluate_tool(ctx, tool, args)
    assert verdict.parks, f"expected {tool.name} to park, got {verdict.as_dict()}"
    return verdict


def _open_approval(
    ctx: LoopContext,
    tool: LoopTool,
    args: Mapping[str, Any],
    *,
    ttl_hours: int | None = None,
) -> Mapping[str, Any]:
    return ensure_approval(
        ctx,
        node=NODE,
        tool=tool,
        args=args,
        business_key=BUSINESS_KEY,
        verdict=_park_verdict(tool, ctx, args),
        ttl_hours=ttl_hours,
    )


def _approve(ctx: LoopContext, row_id: str, *, by: str = HUMAN) -> bool:
    return ctx.approvals.decide(
        row_id,
        state=ApprovalState.APPROVED.value,
        by=by,
        note="looks right",
        at=ctx.clock.now_iso(),
    )


# ---------------------------------------------------------------------------
# 1. The tier floor: T2+ ALWAYS parks, whatever the PolicyPort said
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", [RiskTier.T2, RiskTier.T3])
def test_tier_floor_parks_even_when_the_policy_port_would_allow(tier: RiskTier) -> None:
    """The stricter-of rule, against the most permissive adapter that can exist."""
    permissive = PermissivePolicy()
    ctx = build_memory_context(instance_id="inst-1", template="tpl-1", policy=permissive)
    tool = _register(ctx, "send", tier)

    verdict = evaluate_tool(ctx, tool, {"to": "a@example.com"})

    assert verdict.decision == "park"
    assert not verdict.allows
    assert "loop floor" in verdict.reason
    assert APPROVAL_FLOOR_TIER.name in verdict.reason
    # The adapter WAS consulted. The floor is applied after its answer is read,
    # which is what makes it unreachable from inside an adapter rather than
    # merely undocumented there.
    assert permissive.seen == [tool.resolved_action_class()]


@pytest.mark.parametrize("tier", [RiskTier.T0, RiskTier.T1])
def test_below_the_floor_the_adapter_decides(tier: RiskTier) -> None:
    """An adapter may make T0/T1 stricter — that direction is open on purpose."""
    permissive = build_memory_context(
        instance_id="inst-1", template="tpl-1", policy=PermissivePolicy()
    )
    lenient_tool = _register(permissive, "poke", tier)
    assert evaluate_tool(permissive, lenient_tool, {}).decision == "allow"

    class AlwaysHuman(PermissivePolicy):
        def evaluate(self, action_class: ActionClass) -> PolicyDecision:
            super().evaluate(action_class)
            return PolicyDecision(
                requires_approval=True,
                reason="this shop wants a human on every local write",
                action_class=action_class,
            )

    strict = build_memory_context(instance_id="inst-1", template="tpl-1", policy=AlwaysHuman())
    strict_tool = _register(strict, "poke", tier)
    verdict = evaluate_tool(strict, strict_tool, {})
    assert verdict.decision == "park"
    assert "this shop wants a human" in verdict.reason


def test_always_human_action_class_parks_under_the_shipped_policy(ctx: LoopContext) -> None:
    """A T0 tool that declares ALWAYS_HUMAN parks under the shipped table.

    Note what this does and does not prove. Under ``TierPolicy`` — and under the
    in-memory ``StaticPolicy`` — ALWAYS_HUMAN carries ``requires_approval=True``,
    so the verdict parks. The *tier* floor does not itself see the action class,
    so this guarantee comes from the policy table rather than from the floor.
    """
    tool = _register(ctx, "sign_contract", RiskTier.T0, action_class=ActionClass.ALWAYS_HUMAN)
    verdict = evaluate_tool(ctx, tool, {})
    assert verdict.decision == "park"
    assert verdict.action_class is ActionClass.ALWAYS_HUMAN


# ---------------------------------------------------------------------------
# 2. A PolicyPort that refuses to classify DENIES
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        PolicyError("no row for this action class"),
        ValueError("the adapter's own config file is malformed"),
        RuntimeError("the adapter's database is down"),
    ],
    ids=["policy_error", "value_error", "runtime_error"],
)
def test_a_policy_that_raises_denies_rather_than_assuming_read_only(exc: Exception) -> None:
    """The cheapest escape from the gate would be a typo. It is closed.

    A T0 read-only tool is used deliberately: it is the case where "assume
    read-only" would look most harmless, and it is exactly the case where a
    misspelled action class must not be the way past the gate.
    """
    raising = RaisingPolicy(exc)
    ctx = build_memory_context(instance_id="inst-1", template="tpl-1", policy=raising)
    tool = _register(ctx, "peek", RiskTier.T0)

    verdict = evaluate_tool(ctx, tool, {})

    assert raising.calls == 1
    assert verdict.decision == "deny"
    assert not verdict.allows and not verdict.parks
    assert type(exc).__name__ in verdict.reason or "policy refused" in verdict.reason


def test_a_raising_policy_denies_a_t2_tool_rather_than_parking_it() -> None:
    """Denial is decided before the floor, so a broken adapter cannot mint a row."""
    ctx = build_memory_context(
        instance_id="inst-1", template="tpl-1", policy=RaisingPolicy(PolicyError("nope"))
    )
    tool = _register(ctx, "send", RiskTier.T2)
    assert evaluate_tool(ctx, tool, {}).decision == "deny"


def test_tier_policy_refuses_an_action_class_it_has_no_row_for() -> None:
    """A table lookup that fell back to READ_ONLY would be the whole hole."""
    with pytest.raises(PolicyError) as caught:
        TierPolicy().evaluate("a_class_nobody_declared")  # type: ignore[arg-type]
    assert "refusing to guess" in str(caught.value)


def test_a_zero_hour_ttl_is_refused_by_the_shipped_policy() -> None:
    with pytest.raises(ValueError, match="approval_expiry_hours"):
        TierPolicy(approval_expiry_hours=0)


# ---------------------------------------------------------------------------
# 3. Absence is denial: ungranted, denied, and previewed tools
# ---------------------------------------------------------------------------


def test_an_ungranted_tool_is_denied_and_names_what_is_granted(ctx: LoopContext) -> None:
    _register(ctx, "send", RiskTier.T2)
    stranger = LoopTool(name="wire_money", tier=RiskTier.T1, call=Counted())

    verdict = evaluate_tool(ctx, stranger, {})

    assert verdict.decision == "deny"
    assert "not granted" in verdict.reason
    assert "send" in verdict.reason


def test_a_denied_tool_is_refused_before_the_policy_adapter_is_consulted() -> None:
    """Explicit denial cannot be argued out of by a lenient classification."""
    permissive = PermissivePolicy()
    ctx = build_memory_context(
        instance_id="inst-1",
        template="tpl-1",
        policy=permissive,
        denied_tools=frozenset({"restart"}),
    )
    tool = _register(ctx, "restart", RiskTier.T1)

    verdict = evaluate_tool(ctx, tool, {})

    assert verdict.decision == "deny"
    assert "explicitly denied" in verdict.reason
    assert permissive.seen == [], "the policy adapter must not get a vote on a denial"


def test_preview_of_an_unknown_name_reports_the_worst_class_this_package_knows(
    ctx: LoopContext,
) -> None:
    """An under-reported preview is worse than useless — it reads as reassurance."""
    verdict = preview(ctx, "not_a_tool", {})
    assert verdict.decision == "deny"
    assert verdict.tier is RiskTier.T3
    assert verdict.action_class is ActionClass.IRREVERSIBLE


# ---------------------------------------------------------------------------
# 4. The approval id: deterministic across ticks AND across processes
# ---------------------------------------------------------------------------

_ID_ARGS = ("inst-1", "tpl-1", "send", "mailer", "message-42", "d34db33f")


def test_the_approval_id_is_deterministic_within_a_process() -> None:
    assert approval_id(*_ID_ARGS) == approval_id(*_ID_ARGS)
    assert approval_id(*_ID_ARGS).startswith("apr_")


@pytest.mark.parametrize("hash_seed", ["0", "1", "random"])
def test_the_approval_id_is_deterministic_across_processes(hash_seed: str) -> None:
    """A fresh interpreter, with hash randomisation moved, derives the same id.

    This is not paranoia about sha256. It is a guard against somebody replacing
    the digest with anything that reaches ``hash()``, ``id()``, ``uuid`` or a set
    iteration order — every one of which is stable within a process and different
    in the next one. A durable executor re-runs the code before the park when a
    thread resumes, in a DIFFERENT process, so an id that is only
    within-process-stable pages a human once per tick forever.
    """
    env = {**os.environ, "PYTHONPATH": str(PROTOTYPE_ROOT), "PYTHONHASHSEED": hash_seed}
    script = (
        "import json,sys\n"
        "from selfloop.approvals import approval_id\n"
        f"sys.stdout.write(approval_id(*{_ID_ARGS!r}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == approval_id(*_ID_ARGS)


@pytest.mark.parametrize("axis", range(len(_ID_ARGS)))
def test_every_axis_of_the_id_preimage_distinguishes_two_actions(axis: int) -> None:
    """Drop any one of the six and two different real acts collide on one row."""
    moved = list(_ID_ARGS)
    moved[axis] = f"{moved[axis]}-moved"
    assert approval_id(*moved) != approval_id(*_ID_ARGS)


def test_the_id_separator_makes_the_preimage_unambiguous() -> None:
    """``node='a' + tool='bc'`` must not collide with ``node='ab' + tool='c'``."""
    left = approval_id("inst", "tpl", "a", "bc", "key", "digest")
    right = approval_id("inst", "tpl", "ab", "c", "key", "digest")
    assert left != right


# ---------------------------------------------------------------------------
# 5. One row, one page, ever
# ---------------------------------------------------------------------------


def test_repeated_ticks_create_one_row_and_page_one_human(
    ctx: LoopContext, notifier: RecordingNotifier
) -> None:
    """The replay path of a durable executor re-runs everything before the park."""
    tool = _register(ctx, "send", RiskTier.T2)
    args = {"to": "a@example.com", "body": "hello"}

    rows = [_open_approval(ctx, tool, args) for _ in range(5)]
    row_id = str(rows[0]["approval_id"])

    assert {str(row["approval_id"]) for row in rows} == {row_id}
    assert len(notifier.pages) == 1, "a replayed park must not page a human again"
    assert notifier.pages[0]["approval_id"] == row_id
    assert notifier.pages[0]["deep_link"] == deep_link(ctx, row_id)

    requested = [
        record
        for record in ctx.records.query(RecordKind.DECISION.value)
        if record.get("approval_id") == row_id
    ]
    assert len(requested) == 1, "one durable decision record for one request"
    assert requested[0]["decision"] == ApprovalState.PENDING.value
    assert requested[0]["by"] == ctx.actor


def test_the_row_records_the_full_binding_and_a_redacted_copy_of_the_arguments(
    ctx: LoopContext,
) -> None:
    tool = _register(ctx, "send", RiskTier.T2)
    args = {"to": "a@example.com", "headers": {"Authorization": "Bearer sk-abcdefghijklmnop"}}

    row = _open_approval(ctx, tool, args)

    binding = stored_binding(row)
    assert binding == effect_binding(ctx, NODE, tool, args)
    assert binding["args_digest"] == args_digest(args), (
        "the binding digests the REAL arguments; redaction must not change what a "
        "human's approval is bound to"
    )
    published = row["params"]["args"]
    assert published["headers"]["Authorization"] == "<redacted>"
    assert "sk-abcdefghijklmnop" not in json.dumps(dict(row))
    assert row["state"] == ApprovalState.PENDING.value
    assert row["requested_by"] == ctx.actor


def test_paging_never_raises_and_a_failed_send_is_not_recorded_as_delivered() -> None:
    """A recorded page is the dedupe key. Recording a failed one unpages forever."""
    exploding = ExplodingNotifier()
    ctx = build_memory_context(
        instance_id="inst-1", template="tpl-1", policy=TierPolicy(), notifier=exploding
    )
    tool = _register(ctx, "send", RiskTier.T2)
    verdict = evaluate_tool(ctx, tool, {})

    delivered = page(ctx, "apr_whatever", tool=tool, node=NODE, verdict=verdict)

    assert delivered is False
    assert exploding.attempts == 1
    actions = [event["action"] for event in ctx.events.read()]
    assert "approval_page_failed" in actions
    assert "approval_paged" not in actions


def test_a_deep_link_is_empty_when_no_dashboard_origin_was_configured() -> None:
    ctx = build_memory_context(instance_id="inst-1", template="tpl-1")
    assert deep_link(ctx, "apr_x") == ""


def test_an_unparseable_clock_refuses_to_mint_an_approval_deadline() -> None:
    """A row whose deadline every reader treats as passed is worse than no row."""
    ctx = build_memory_context(
        instance_id="inst-1", template="tpl-1", policy=TierPolicy(), clock=BrokenClock()
    )
    tool = _register(ctx, "send", RiskTier.T2)

    with pytest.raises(LoopError) as caught:
        _open_approval(ctx, tool, {})

    assert "deadline" in str(caught.value)
    assert ctx.approvals.get(approval_id("inst-1", "tpl-1", NODE, "send", BUSINESS_KEY,
                                         args_digest({}))) is None


# ---------------------------------------------------------------------------
# 6. Changed arguments: invalidation, and then a NEW row rather than a deadlock
# ---------------------------------------------------------------------------


def test_changing_one_argument_invalidates_the_human_decision(ctx: LoopContext) -> None:
    """A human approves an ACTION. The binding re-check reads no clock at all."""
    tool = _register(ctx, "send", RiskTier.T2)
    approved_args = {"to": "a@example.com", "amount": 10}
    swapped_args = {"to": "a@example.com", "amount": 10_000}

    row = _open_approval(ctx, tool, approved_args)
    row_id = str(row["approval_id"])
    assert _approve(ctx, row_id)

    honoured = read_outcome(ctx, row_id, binding=effect_binding(ctx, NODE, tool, approved_args))
    assert honoured.approved is True

    refused = read_outcome(ctx, row_id, binding=effect_binding(ctx, NODE, tool, swapped_args))
    assert refused.approved is False
    assert refused.terminal is True
    assert "different action" in refused.reason
    assert "args_digest" in refused.reason


def test_changed_arguments_mint_a_new_pending_row_rather_than_deadlocking(
    ctx: LoopContext, notifier: RecordingNotifier
) -> None:
    """FIX-10. The old row stays decided; the new act parks on a row of its own.

    Without the argument digest in the id preimage, the second call re-derives the
    FIRST row's id, hands back a row whose binding can never match, and the effect
    parks forever against a decision that was made about something else.
    """
    tool = _register(ctx, "send", RiskTier.T2)
    approved_args = {"to": "a@example.com", "amount": 10}
    swapped_args = {"to": "a@example.com", "amount": 10_000}

    first = _open_approval(ctx, tool, approved_args)
    first_id = str(first["approval_id"])
    assert _approve(ctx, first_id)

    second = _open_approval(ctx, tool, swapped_args)
    second_id = str(second["approval_id"])

    assert second_id != first_id
    assert second["state"] == ApprovalState.PENDING.value
    assert len(notifier.pages) == 2, "a genuinely new action pages a human once"

    # The new row is pending, which is the only NON-terminal reading in the whole
    # module: there is something left to wait for, so the tick parks again.
    waiting = read_outcome(ctx, second_id, binding=effect_binding(ctx, NODE, tool, swapped_args))
    assert waiting.approved is False
    assert waiting.terminal is False

    # And the first decision is untouched: a human's answer about one action is
    # not consumed, invalidated or re-opened by a different action.
    still_approved = read_outcome(
        ctx, first_id, binding=effect_binding(ctx, NODE, tool, approved_args)
    )
    assert still_approved.approved is True


def test_binding_drift_under_a_fixed_id_refuses_rather_than_parking_in_silence(
    ctx: LoopContext,
) -> None:
    """The id is unchanged, the binding is not: a row that could never authorise.

    Reached by re-declaring the tool's action class, which is outside the id's
    preimage. Returning the stale row would park forever with nothing in the
    record explaining why; a refusal reaches an operator.
    """
    args = {"to": "a@example.com"}
    declared = _register(ctx, "send", RiskTier.T2, action_class=ActionClass.CONSEQUENTIAL)
    _open_approval(ctx, declared, args)

    redeclared = LoopTool(
        name="send",
        tier=RiskTier.T2,
        call=Counted(),
        action_class=ActionClass.IRREVERSIBLE,
        description="send something",
    )
    with pytest.raises(LoopError) as caught:
        ensure_approval(
            ctx,
            node=NODE,
            tool=redeclared,
            args=args,
            business_key=BUSINESS_KEY,
            verdict=evaluate_tool(ctx, redeclared, args),
        )
    assert "bound to a different action" in str(caught.value)
    assert "action_class" in str(caught.value)


# ---------------------------------------------------------------------------
# 7. Expiry aborts; it never approves
# ---------------------------------------------------------------------------


def test_an_expired_but_approved_row_is_not_resume_authority(ctx: LoopContext) -> None:
    """Expiry is checked BEFORE the state, so ``approved`` past its deadline is not."""
    tool = _register(ctx, "send", RiskTier.T2)
    row = _open_approval(ctx, tool, {"to": "a@example.com"}, ttl_hours=1)
    row_id = str(row["approval_id"])
    assert _approve(ctx, row_id)
    assert read_outcome(ctx, row_id).approved is True

    ctx.clock.advance(3601)

    late = read_outcome(ctx, row_id)
    assert late.approved is False
    assert late.terminal is True
    assert late.state == ApprovalState.EXPIRED.value
    assert "standing permission" in late.reason
    # The STORE still says approved. The refusal is a property of the reader, not
    # of a background job that may or may not have run.
    assert ctx.approvals.get(row_id)["state"] == ApprovalState.APPROVED.value


def test_an_unreadable_deadline_counts_as_expired(ctx: LoopContext) -> None:
    ctx.approvals.create(
        {
            "approval_id": "apr_unreadable",
            "state": ApprovalState.APPROVED.value,
            "decided_by": HUMAN,
            "expires_at": "some time next week",
        }
    )
    outcome = read_outcome(ctx, "apr_unreadable")
    assert outcome.approved is False
    assert outcome.state == ApprovalState.EXPIRED.value


def test_resolve_for_resume_closes_an_expired_pending_row(ctx: LoopContext) -> None:
    """The one write in the module, and it only makes the store agree with readers."""
    tool = _register(ctx, "send", RiskTier.T2)
    row_id = str(_open_approval(ctx, tool, {}, ttl_hours=1)["approval_id"])
    ctx.clock.advance(3601)

    outcome = resolve_for_resume(ctx, row_id)

    assert outcome.approved is False
    assert outcome.state == ApprovalState.EXPIRED.value
    assert ctx.approvals.get(row_id)["state"] == ApprovalState.EXPIRED.value
    closures = [
        record
        for record in ctx.records.query(RecordKind.DECISION.value)
        if record.get("approval_id") == row_id
        and record["decision"] == ApprovalState.EXPIRED.value
    ]
    assert len(closures) == 1


def test_resolve_for_resume_does_not_disturb_a_live_pending_row(ctx: LoopContext) -> None:
    tool = _register(ctx, "send", RiskTier.T2)
    row_id = str(_open_approval(ctx, tool, {}, ttl_hours=8)["approval_id"])

    outcome = resolve_for_resume(ctx, row_id)

    assert outcome.terminal is False
    assert outcome.state == ApprovalState.PENDING.value
    assert ctx.approvals.get(row_id)["state"] == ApprovalState.PENDING.value


def test_the_default_ttl_comes_from_the_policy_port(ctx: LoopContext) -> None:
    """Expiry is a policy statement, so it lives with the other policy statements."""
    tool = _register(ctx, "send", RiskTier.T2)
    row = _open_approval(ctx, tool, {})
    assert TierPolicy().approval_expiry_hours == DEFAULT_TTL_HOURS
    ctx.clock.advance(DEFAULT_TTL_HOURS * 3600 - 5)
    assert read_outcome(ctx, str(row["approval_id"])).state == ApprovalState.PENDING.value
    ctx.clock.advance(10)
    assert read_outcome(ctx, str(row["approval_id"])).state == ApprovalState.EXPIRED.value


# ---------------------------------------------------------------------------
# 8. Only a human decides — and the loop has no string that would satisfy it
# ---------------------------------------------------------------------------


def test_the_loops_own_actor_can_never_satisfy_an_approval(ctx: LoopContext) -> None:
    """Self-approval is structurally impossible, not merely against the rules.

    ``LoopContext.actor`` is ``loop:<instance>`` and there is no other string the
    loop can put in a decision field, so this test writes the very best forgery
    the runtime is capable of and watches it fail.
    """
    tool = _register(ctx, "send", RiskTier.T2)
    row_id = str(_open_approval(ctx, tool, {})["approval_id"])

    assert ctx.actor == "loop:inst-1"
    assert _approve(ctx, row_id, by=ctx.actor) is True, "the CAS itself succeeds"

    outcome = read_outcome(ctx, row_id)
    assert outcome.approved is False
    assert outcome.terminal is True
    assert outcome.decided_by == ctx.actor
    assert "must be decided by a person" in outcome.reason


@pytest.mark.parametrize("prefix", AUTOMATION_IDENTITY_PREFIXES)
def test_no_automation_identity_can_decide_a_loop_approval(
    ctx: LoopContext, prefix: str
) -> None:
    tool = _register(ctx, "send", RiskTier.T2)
    row_id = str(_open_approval(ctx, tool, {})["approval_id"])
    assert _approve(ctx, row_id, by=f"{prefix}nightly-runner")
    assert read_outcome(ctx, row_id).approved is False


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_decider_is_not_a_person(ctx: LoopContext, blank: str) -> None:
    tool = _register(ctx, "send", RiskTier.T2)
    row_id = str(_open_approval(ctx, tool, {})["approval_id"])
    assert _approve(ctx, row_id, by=blank)
    assert read_outcome(ctx, row_id).approved is False


def test_a_missing_row_is_a_refusal_and_a_read_mints_nothing(ctx: LoopContext) -> None:
    outcome = read_outcome(ctx, "apr_never_existed")
    assert outcome.approved is False
    assert outcome.terminal is True
    assert outcome.state == "missing"
    assert ctx.approvals.get("apr_never_existed") is None


def test_a_state_this_package_does_not_recognise_is_not_authority(ctx: LoopContext) -> None:
    ctx.approvals.create(
        {
            "approval_id": "apr_drifted",
            "state": "granted",
            "decided_by": HUMAN,
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    outcome = read_outcome(ctx, "apr_drifted")
    assert outcome.approved is False
    assert outcome.terminal is True
    assert "does not recognise" in outcome.reason


@pytest.mark.parametrize(
    "state", [ApprovalState.REJECTED.value, ApprovalState.PENDING.value]
)
def test_only_an_approved_state_is_ever_read_as_approved(
    ctx: LoopContext, state: str
) -> None:
    ctx.approvals.create(
        {
            "approval_id": f"apr_{state}",
            "state": state,
            "decided_by": HUMAN if state != ApprovalState.PENDING.value else "",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    outcome = read_outcome(ctx, f"apr_{state}")
    assert outcome.approved is False
    assert outcome.terminal is (state != ApprovalState.PENDING.value)


# ---------------------------------------------------------------------------
# 9. The execution seam re-checks everything the gate decided
# ---------------------------------------------------------------------------


def test_an_approved_action_executes_exactly_once_through_the_seam(ctx: LoopContext) -> None:
    tool = _register(ctx, "send", RiskTier.T2)
    counter = _implementation(ctx, "send")
    args = {"to": "a@example.com", "body": "hello"}
    row_id = str(_open_approval(ctx, tool, args)["approval_id"])
    assert _approve(ctx, row_id)

    outcome = execute_effect(
        ctx,
        node=NODE,
        tool=tool,
        args=args,
        business_key=BUSINESS_KEY,
        gate_token={"approval_id": row_id},
    )

    assert outcome["succeeded"] is True
    assert outcome["replayed"] is False
    assert len(counter.calls) == 1
    assert counter.calls[0] == args


def test_the_seam_refuses_a_changed_payload_and_never_reaches_the_tool(
    ctx: LoopContext,
) -> None:
    """The assertion that matters is the call counter, not the exception type."""
    tool = _register(ctx, "send", RiskTier.T2)
    counter = _implementation(ctx, "send")
    approved_args = {"to": "a@example.com", "amount": 10}
    swapped_args = {"to": "a@example.com", "amount": 10_000}
    row_id = str(_open_approval(ctx, tool, approved_args)["approval_id"])
    assert _approve(ctx, row_id)

    with pytest.raises(EffectNotApproved):
        execute_effect(
            ctx,
            node=NODE,
            tool=tool,
            args=swapped_args,
            business_key=BUSINESS_KEY,
            gate_token={"approval_id": row_id},
        )

    assert counter.calls == []


def test_the_seam_refuses_a_t2_effect_that_never_parked(ctx: LoopContext) -> None:
    """A missing gate token means the loop did not stop and be seen stopping."""
    tool = _register(ctx, "send", RiskTier.T2)
    counter = _implementation(ctx, "send")
    args = {"to": "a@example.com"}
    row_id = str(_open_approval(ctx, tool, args)["approval_id"])
    assert _approve(ctx, row_id)

    with pytest.raises(EffectNotApproved, match="no gate token"):
        execute_effect(
            ctx, node=NODE, tool=tool, args=args, business_key=BUSINESS_KEY, gate_token=None
        )
    assert counter.calls == []


def test_the_seam_refuses_an_expired_approval_it_was_handed_a_token_for(
    ctx: LoopContext,
) -> None:
    tool = _register(ctx, "send", RiskTier.T2)
    counter = _implementation(ctx, "send")
    args = {"to": "a@example.com"}
    row_id = str(_open_approval(ctx, tool, args, ttl_hours=1)["approval_id"])
    assert _approve(ctx, row_id)
    ctx.clock.advance(3601)

    with pytest.raises(EffectNotApproved, match="expired"):
        execute_effect(
            ctx,
            node=NODE,
            tool=tool,
            args=args,
            business_key=BUSINESS_KEY,
            gate_token={"approval_id": row_id},
        )
    assert counter.calls == []


def test_read_mode_keeps_the_t0_and_allow_guard(ctx: LoopContext) -> None:
    """FIX-18. A read is exempt from the receipt, never from the policy."""
    reversible = _register(ctx, "restart", RiskTier.T1)
    with pytest.raises(EffectDenied, match="read mode requires a T0 tool"):
        execute_effect(
            ctx, node="poll", tool=reversible, args={}, business_key="", mode="read"
        )
    assert _implementation(ctx, "restart").calls == []

    readonly = _register(ctx, "peek", RiskTier.T0)
    result = execute_effect(
        ctx, node="poll", tool=readonly, args={}, business_key="", mode="read"
    )
    assert result["receipt"] is None
    assert result["replayed"] is False
    assert "succeeded" not in result, (
        "a read returns three keys and not seven; inventing succeeded=True would let a "
        "caller treat an unreceipted call as a receipted effect"
    )
    assert len(_implementation(ctx, "peek").calls) == 1


def test_a_denied_tool_is_refused_at_the_seam_before_the_callable(ctx: LoopContext) -> None:
    stranger = LoopTool(name="wire_money", tier=RiskTier.T1, call=Counted())
    with pytest.raises(EffectDenied):
        execute_effect(
            ctx, node=NODE, tool=stranger, args={}, business_key=BUSINESS_KEY
        )
    assert stranger.call.calls == []  # type: ignore[attr-defined]


def _implementation(ctx: LoopContext, name: str) -> Counted:
    """The :class:`Counted` behind a granted tool's sealed handle.

    Reaching for it through the closure cell is exactly the access the module
    docstring of ``selfloop.tools`` says the seal does NOT close, and using it
    here is the honest way to write these assertions: the test needs to know
    whether the implementation ran, and asking the implementation is the only
    answer that is not itself a claim by the code under test.
    """
    sealed = ctx.tools.get(name).call
    for cell in sealed.__closure__ or ():
        if isinstance(cell.cell_contents, Counted):
            return cell.cell_contents
    raise AssertionError(f"no Counted implementation found behind tool {name!r}")


# ---------------------------------------------------------------------------
# 10. Redaction reaches all the way down
# ---------------------------------------------------------------------------


def test_redaction_reaches_a_nested_authorization_header() -> None:
    """The single most common way a credential appears in a connector call.

    Inspecting only TOP-LEVEL keys meant this exact shape was stringified whole
    and written verbatim onto the row, which the approvals page then renders to
    whoever opens it.
    """
    redacted = redact_args(
        {"request": {"headers": {"Authorization": "Bearer sk-abcdefghijklmnop"}}}
    )
    assert redacted["request"]["headers"]["Authorization"] == "<redacted>"
    assert "sk-abcdefghijklmnop" not in json.dumps(redacted)


def test_redaction_matches_by_value_shape_when_the_key_is_innocuous() -> None:
    """Two independent tests, because either one alone leaks."""
    redacted = redact_args(
        {
            "command": "curl -H 'Authorization: Bearer ghp_0123456789abcdefghij' https://x",
            "note": "the webhook is https://hooks.slack.com/services/T000/B000/xxxxxxxx",
        }
    )
    blob = json.dumps(redacted)
    assert "ghp_0123456789abcdefghij" not in blob
    assert "hooks.slack.com/services" not in blob
    assert "<redacted>" in redacted["command"]


@pytest.mark.parametrize(
    "key", ["api_key", "SECRET_TOKEN", "password", "session_id", "private_pem", "cookie"]
)
def test_redaction_matches_by_key_name_at_any_depth(key: str) -> None:
    redacted = redact_args({"outer": {"inner": {key: "hunter2-hunter2"}}})
    assert redacted["outer"]["inner"][key] == "<redacted>"


def test_redaction_keeps_the_shape_a_human_needs_to_decide() -> None:
    """A page nobody can read is a page nobody answers."""
    redacted = redact_args(
        {"to": "a@example.com", "count": 3, "dry_run": False, "tags": ["urgent", "billing"]}
    )
    assert redacted == {
        "to": "a@example.com",
        "count": 3,
        "dry_run": False,
        "tags": ["urgent", "billing"],
    }


def test_redaction_truncates_a_blob_rather_than_publishing_it() -> None:
    redacted = redact_args({"body": "x" * 5000})
    assert len(redacted["body"]) < 500
    assert redacted["body"].endswith("…")


def test_redaction_terminates_on_a_self_referential_payload() -> None:
    """The depth cap doubles as the cycle guard, and this is why it has to."""
    cyclic: dict[str, Any] = {"name": "loop"}
    cyclic["self"] = cyclic

    redacted = redact_args({"payload": cyclic})

    rendered = json.dumps(redacted)
    assert "<depth>" in rendered
