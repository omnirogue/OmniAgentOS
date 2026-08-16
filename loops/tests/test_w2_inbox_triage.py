"""W2 Inbox Triage: test classification, drafting, approval-gated sends, and crash-resume."""

from __future__ import annotations

from typing import Any

from conftest import Counter, tool
from omniagentos_loops.approvals import LOOP_APPROVAL_KIND
from omniagentos_loops.contracts import LoopStatus, RiskTier
from omniagentos_loops.runtime import run_once
from omniagentos_loops.templates import get_template
from omniagentos_loops.tools import ToolRegistry

from omniagentos.contracts import ApprovalState

TEMPLATE = get_template("poll_classify_act_verify")


# ============================================================================
# Test Helpers
# ============================================================================


def _w2_ctx(make_ctx, messages: list[dict[str, Any]], classify_result: dict[str, Any], act: Counter):
    """Setup W2 context with poll, classify, act, verify."""
    registry = ToolRegistry()
    registry.register(tool("poll", RiskTier.T0, Counter(result=messages)))
    registry.register(tool("classify", RiskTier.T0, Counter(result=classify_result)))
    # act is T2: the Counter that will be checked
    registry.register(tool("act", RiskTier.T2, act, key=lambda args: f"{args['item'].get('id')}:{args['decision'].get('action')}"))
    registry.register(tool("verify", RiskTier.T0, Counter(result={"verified": True})))
    return make_ctx(instance_id="w2_inbox", template=TEMPLATE.name, tools=registry)


def _rows(store: Any) -> list[dict[str, Any]]:
    """Fetch all approvals rows from the real control-plane DB."""
    return [dict(r) for r in store._connection.execute("SELECT * FROM approvals").fetchall()]


# ============================================================================
# Classification Tests
# ============================================================================


def test_w2_reply_action_creates_approval_row(make_ctx, store):
    """Reply classification creates an approval row (real bridge)."""
    messages = [{"id": 1, "from_": "customer@example.com", "subject": "Help?", "body": "..."}]
    classify_result = {"action": "reply", "confidence": 0.95}

    act = Counter()
    ctx = _w2_ctx(make_ctx, messages, classify_result, act)
    report = run_once(ctx, TEMPLATE)

    # First tick: should park for approval
    assert report.status is LoopStatus.PARKED
    assert act.count == 0, "act should not execute before approval"

    # Verify real approval row exists in the DB
    rows = _rows(store)
    assert len(rows) == 1, "should have exactly one approval"
    assert rows[0]["state"] == ApprovalState.PENDING.value
    assert rows[0]["risk"] == LOOP_APPROVAL_KIND
    assert rows[0]["id"] == report.approval_id


def test_w2_skip_action_does_not_create_approval(make_ctx, store):
    """Skip classification (archive/ignore) routes to idle, no approval row."""
    messages = [{"id": 1, "from_": "noreply@stripe.com", "subject": "Receipt", "body": "..."}]
    classify_result = {"action": "skip", "reason": "auto-generated"}

    act = Counter()
    ctx = _w2_ctx(make_ctx, messages, classify_result, act)
    report = run_once(ctx, TEMPLATE)

    # Template treats "skip" as no-op
    assert report.status is LoopStatus.IDLE
    assert act.count == 0, "act should not be called for skip"

    # No approval rows should exist
    rows = _rows(store)
    assert len(rows) == 0, "skip should not create approvals"


# ============================================================================
# Approval Workflow Tests (Real Bridge)
# ============================================================================


def test_w2_approval_then_send(make_ctx, store):
    """Approval row is honored: approved → verify runs; rejected → abort."""
    messages = [{"id": 1, "from_": "customer@example.com", "subject": "Order?", "body": "..."}]
    classify_result = {"action": "reply", "confidence": 0.95}

    act = Counter(result={"draft": {"to": "customer@example.com", "subject": "Re: Order?", "body": "..."}})
    verify = Counter(result={"verified": True, "sent": True})

    registry = ToolRegistry()
    registry.register(tool("poll", RiskTier.T0, Counter(result=messages)))
    registry.register(tool("classify", RiskTier.T0, Counter(result=classify_result)))
    registry.register(tool("act", RiskTier.T2, act, key=lambda args: f"msg-{args['item'].get('id')}"))
    registry.register(tool("verify", RiskTier.T0, verify))
    ctx = make_ctx(instance_id="w2_inbox", template=TEMPLATE.name, tools=registry)

    # Tick 1: park for approval
    parked = run_once(ctx, TEMPLATE)
    assert parked.status is LoopStatus.PARKED
    assert act.count == 0
    assert verify.count == 0

    # Approve the message
    store.decide_approval(parked.approval_id, ApprovalState.APPROVED.value, "owner", "send it")

    # Tick 2: resume and execute
    resumed = run_once(ctx, TEMPLATE)
    assert resumed.status is LoopStatus.COMPLETED
    # Real bridge: act is called when approved
    assert act.count == 1, "approved message should execute act"
    # Verify is also called (it's the post-act step)
    assert verify.count == 1, "verify should run after act"


def test_w2_expiry_prevents_send(make_ctx, store):
    """Expired approval halts execution (real bridge fail-closed)."""
    messages = [{"id": 1, "from_": "customer@example.com", "subject": "Q?", "body": "..."}]
    classify_result = {"action": "reply", "confidence": 0.95}

    act = Counter(result={"draft": {"to": "x@y.com", "subject": "Re: Q?", "body": "..."}})

    registry = ToolRegistry()
    registry.register(tool("poll", RiskTier.T0, Counter(result=messages)))
    registry.register(tool("classify", RiskTier.T0, Counter(result=classify_result)))
    registry.register(tool("act", RiskTier.T2, act))
    registry.register(tool("verify", RiskTier.T0, Counter(result={"verified": True, "sent": True})))
    ctx = make_ctx(instance_id="w2_inbox", template=TEMPLATE.name, tools=registry)

    # Tick 1: park
    parked = run_once(ctx, TEMPLATE)
    assert parked.status is LoopStatus.PARKED

    # Set approval to expired
    store._connection.execute(
        "UPDATE approvals SET expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00Z", parked.approval_id),
    )
    store._connection.commit()

    # Tick 2: resume but abort (expired)
    resumed = run_once(ctx, TEMPLATE)
    assert resumed.status is LoopStatus.ABORTED, "expired approval should abort"
    assert act.count == 0, "act should not run when expired"


def test_w2_rejection_prevents_send(make_ctx, store):
    """Rejected approval halts execution."""
    messages = [{"id": 1, "from_": "customer@example.com", "subject": "Q?", "body": "..."}]
    classify_result = {"action": "reply", "confidence": 0.95}

    act = Counter(result={"draft": {"to": "x@y.com", "subject": "Re: Q?", "body": "..."}})

    registry = ToolRegistry()
    registry.register(tool("poll", RiskTier.T0, Counter(result=messages)))
    registry.register(tool("classify", RiskTier.T0, Counter(result=classify_result)))
    registry.register(tool("act", RiskTier.T2, act))
    registry.register(tool("verify", RiskTier.T0, Counter(result={"verified": True, "sent": True})))
    ctx = make_ctx(instance_id="w2_inbox", template=TEMPLATE.name, tools=registry)

    # Tick 1: park
    parked = run_once(ctx, TEMPLATE)
    assert parked.status is LoopStatus.PARKED

    # Reject the approval
    store.decide_approval(parked.approval_id, ApprovalState.REJECTED.value, "owner", "nope")

    # Tick 2: resume but abort (rejected)
    resumed = run_once(ctx, TEMPLATE)
    assert resumed.status is LoopStatus.ABORTED, "rejected approval should abort"
    assert act.count == 0, "act should not run when rejected"


# ============================================================================
# Determinism Tests
# ============================================================================


def test_w2_approval_id_deterministic(make_ctx, store):
    """Approval IDs are deterministic per (instance, node, business-key)."""
    messages = [{"id": 1, "from_": "customer@example.com", "subject": "Q?", "body": "..."}]
    classify_result = {"action": "reply", "confidence": 0.95}

    act1 = Counter()
    ctx1 = _w2_ctx(make_ctx, messages, classify_result, act1)
    report1 = run_once(ctx1, TEMPLATE)
    assert report1.status is LoopStatus.PARKED
    id1 = report1.approval_id

    # Re-tick the same context (same message, same decision)
    act2 = Counter()
    ctx2 = _w2_ctx(make_ctx, messages, classify_result, act2)
    report2 = run_once(ctx2, TEMPLATE)
    assert report2.status is LoopStatus.PARKED
    id2 = report2.approval_id

    # Should be the same approval (deduped)
    assert id1 == id2, "same message+decision should have same approval ID"

    # DB should still have only one row
    rows = _rows(store)
    assert len(rows) == 1, "re-ticking should not duplicate the approval"


def test_w2_retick_while_undecided_does_not_re_page(make_ctx, store, notifier):
    """Multiple ticks while undecided don't re-page the human (real bridge dedup)."""
    messages = [{"id": 1, "from_": "customer@example.com", "subject": "Q?", "body": "..."}]
    classify_result = {"action": "reply", "confidence": 0.95}

    act = Counter()
    ctx = _w2_ctx(make_ctx, messages, classify_result, act)

    # Tick 1: page human
    run_once(ctx, TEMPLATE)
    assert len(notifier.pages) == 1

    # Tick 2: same approval, should not re-page
    run_once(ctx, TEMPLATE)
    assert len(notifier.pages) == 1, "re-tick should not re-page"


# ============================================================================
# Idle Tests
# ============================================================================


def test_w2_idle_with_no_messages(make_ctx):
    """Empty poll routes to idle."""
    registry = ToolRegistry()
    registry.register(tool("poll", RiskTier.T0, Counter(result=[])))
    registry.register(tool("classify", RiskTier.T0, Counter(result={"action": "reply"})))
    act = Counter()
    registry.register(tool("act", RiskTier.T2, act))
    registry.register(tool("verify", RiskTier.T0, Counter(result={"verified": True})))

    ctx = make_ctx(instance_id="w2_inbox", template=TEMPLATE.name, tools=registry)
    report = run_once(ctx, TEMPLATE)

    assert report.status is LoopStatus.IDLE
    assert act.count == 0


# ============================================================================
# Counterfeit: Skip→Reply Mutation
# ============================================================================


def test_w2_counterfeit_skip_becomes_reply(make_ctx, store):
    """Counterfeit: classify returns 'skip' but we mutate to 'reply'.

    This must be caught: skip routes to idle (no approval), but reply creates approval.
    The presence of an approval row when we expect none detects the mutation.
    """
    messages = [{"id": 1, "from_": "noreply@stripe.com", "subject": "Receipt", "body": "..."}]
    # Correct decision is "skip" (no-reply), but we'll mutate it
    classify_result = {"action": "reply"}  # COUNTERFEIT: should be "skip"

    act = Counter()
    ctx = _w2_ctx(make_ctx, messages, classify_result, act)
    report = run_once(ctx, TEMPLATE)

    # Counterfeit causes reply path → parks for approval
    assert report.status is LoopStatus.PARKED
    rows = _rows(store)
    assert len(rows) == 1, "mutation forces approval creation"

    # The counterfeit test proves that changing skip→reply creates observable side effects
    # (approval row exists when it shouldn't)
