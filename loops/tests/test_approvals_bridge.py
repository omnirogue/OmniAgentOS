"""interrupt <-> approvals-row round trip, end to end through a real graph.

Every assertion pairs a control-plane fact (the approvals row / its state) with
the only thing that actually matters: how many times the outbound tool ran.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import Counter, tool
from omniagentos_loops.approvals import LOOP_APPROVAL_KIND, approval_id
from omniagentos_loops.contracts import LoopStatus, RiskTier
from omniagentos_loops.runtime import run_once
from omniagentos_loops.templates import get_template
from omniagentos_loops.tools import ToolRegistry

from omniagentos.contracts import ApprovalState, utc_now_iso

TEMPLATE = get_template("draft_approve_send")
DRAFT = {"to": "customer@example.com", "subject": "Re: order", "body": "on its way"}


def _ctx(make_ctx, send: Counter, draft: dict[str, Any] | None = DRAFT) -> Any:
    registry = ToolRegistry()
    registry.register(tool("draft", RiskTier.T0, Counter(result=draft)))
    registry.register(tool("send", RiskTier.T2, send, key=lambda args: str(args["draft"]["to"])))
    return make_ctx(instance_id="cs_replies", template=TEMPLATE.name, tools=registry)


def _rows(store: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in store._connection.execute("SELECT * FROM approvals").fetchall()]


def test_first_tick_parks_and_creates_exactly_one_pending_row(make_ctx, store, notifier):
    send = Counter()
    ctx = _ctx(make_ctx, send)

    report = run_once(ctx, TEMPLATE)

    assert report.status is LoopStatus.PARKED
    assert send.count == 0
    rows = _rows(store)
    assert len(rows) == 1
    assert rows[0]["state"] == ApprovalState.PENDING.value
    assert rows[0]["risk"] == LOOP_APPROVAL_KIND
    assert rows[0]["id"] == report.approval_id
    assert rows[0]["expires_at"]
    assert notifier.pages and "/approvals?approval=" in notifier.pages[0]


def test_reticking_while_undecided_does_not_duplicate_the_row_or_page_twice(
    make_ctx, store, notifier
):
    send = Counter()
    ctx = _ctx(make_ctx, send)
    run_once(ctx, TEMPLATE)
    second = run_once(ctx, TEMPLATE)

    assert second.status is LoopStatus.PARKED
    assert send.count == 0
    assert len(_rows(store)) == 1, "the deterministic approval id must dedupe"
    assert len(notifier.pages) == 1, "a re-tick must not re-page a human"


def test_human_approval_resumes_the_graph_and_sends_once(make_ctx, store):
    send = Counter()
    ctx = _ctx(make_ctx, send)
    parked = run_once(ctx, TEMPLATE)

    assert store.decide_approval(parked.approval_id, ApprovalState.APPROVED.value, "owner", "ok")

    resumed = run_once(ctx, TEMPLATE)
    assert resumed.status is LoopStatus.COMPLETED
    assert resumed.resumed is True
    assert send.count == 1
    assert send.calls[0]["draft"] == DRAFT


def test_rejection_aborts_without_sending(make_ctx, store):
    send = Counter()
    ctx = _ctx(make_ctx, send)
    parked = run_once(ctx, TEMPLATE)
    store.decide_approval(parked.approval_id, ApprovalState.REJECTED.value, "owner", "no")

    report = run_once(ctx, TEMPLATE)
    assert report.status is LoopStatus.ABORTED
    assert send.count == 0


def test_expiry_aborts_and_never_approves(make_ctx, store):
    send = Counter()
    ctx = _ctx(make_ctx, send)
    parked = run_once(ctx, TEMPLATE)

    store._connection.execute(
        "UPDATE approvals SET expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00Z", parked.approval_id),
    )
    store._connection.commit()

    report = run_once(ctx, TEMPLATE)
    assert report.status is LoopStatus.ABORTED
    assert send.count == 0
    row = next(r for r in _rows(store) if r["id"] == parked.approval_id)
    assert row["state"] == ApprovalState.EXPIRED.value
    assert row["decided_by"] == "system:loops"


def test_late_approval_on_an_expired_row_is_not_authority(make_ctx, store):
    """T-CODE-002: expiry binds even if a human clicks approve afterwards."""
    send = Counter()
    ctx = _ctx(make_ctx, send)
    parked = run_once(ctx, TEMPLATE)

    store._connection.execute(
        "UPDATE approvals SET expires_at = ?, state = ?, decided_by = ? WHERE id = ?",
        ("2000-01-01T00:00:00Z", ApprovalState.APPROVED.value, "owner", parked.approval_id),
    )
    store._connection.commit()

    report = run_once(ctx, TEMPLATE)
    assert report.status is LoopStatus.ABORTED
    assert send.count == 0


@pytest.mark.parametrize("decider", ["bot:ci", "system", "runner:w1", "release-bot"])
def test_an_automation_identity_cannot_satisfy_a_loop_approval(make_ctx, store, decider):
    send = Counter()
    ctx = _ctx(make_ctx, send)
    parked = run_once(ctx, TEMPLATE)
    store.decide_approval(parked.approval_id, ApprovalState.APPROVED.value, decider, "auto")

    report = run_once(ctx, TEMPLATE)
    assert report.status is LoopStatus.ABORTED
    assert send.count == 0


def test_approval_id_is_deterministic_and_scoped_on_every_axis():
    """Determinism makes creation idempotent; scoping stops confused deputies."""
    first = approval_id("inst", "tpl", "send", "email", "msg-1")
    assert first == approval_id("inst", "tpl", "send", "email", "msg-1")
    assert first.startswith("apr_") and len(first) == len("apr_") + 20

    # Any axis that can distinguish two real-world actions must change the id.
    assert first != approval_id("other", "tpl", "send", "email", "msg-1")
    assert first != approval_id("inst", "tpl2", "send", "email", "msg-1")
    assert first != approval_id("inst", "tpl", "send2", "email", "msg-1")
    assert first != approval_id("inst", "tpl", "send", "sms", "msg-1")
    assert first != approval_id("inst", "tpl", "send", "email", "msg-2")


def test_the_approval_row_records_the_binding_it_authorises(make_ctx, store):
    from omniagentos_loops.approvals import stored_binding

    ctx = _ctx(make_ctx, Counter())
    parked = run_once(ctx, TEMPLATE)
    row = next(r for r in _rows(store) if r["id"] == parked.approval_id)
    binding = stored_binding(row)
    assert binding["instance"] == "cs_replies"
    assert binding["template"] == TEMPLATE.name
    assert binding["node"] == "send"
    assert binding["tool"] == "send"
    assert binding["action_class"] == "consequential"
    assert binding["args_digest"]


def test_an_approval_does_not_carry_to_a_different_instance(make_ctx, store):
    """Confused deputy: instance A's approval must not authorise instance B."""
    send_a, send_b = Counter(), Counter()
    ctx_a = _ctx(make_ctx, send_a)
    parked = run_once(ctx_a, TEMPLATE)
    store.decide_approval(parked.approval_id, ApprovalState.APPROVED.value, "owner", "ok")

    ctx_b = _ctx(make_ctx, send_b)
    ctx_b.instance_id = "cs_replies_other"
    report = run_once(ctx_b, TEMPLATE)

    assert report.status is LoopStatus.PARKED, "B must raise its OWN approval"
    assert report.approval_id != parked.approval_id
    assert send_b.count == 0

    # And A still completes on its own approval.
    assert run_once(ctx_a, TEMPLATE).status is LoopStatus.COMPLETED
    assert send_a.count == 1


def test_a_revised_message_needs_its_own_approval(make_ctx, store):
    """A human approved a payload, not a slot.

    ``draft_approve_send`` binds its business key to recipient + content, so a
    revised draft is a genuinely new effect: it must raise a NEW approval rather
    than ride the one a human granted for the earlier text.
    """
    send = Counter()
    registry = ToolRegistry()
    drafts = [
        {"to": "customer@example.com", "subject": "Re: order", "body": "on its way"},
        {"to": "customer@example.com", "subject": "Re: order", "body": "TOTALLY DIFFERENT"},
    ]
    seen: list[int] = []

    def draft_tool(**kwargs):
        current = drafts[min(len(seen), len(drafts) - 1)]
        seen.append(1)
        return current

    registry.register(tool("draft", RiskTier.T0, draft_tool))
    registry.register(tool("send", RiskTier.T2, send))
    ctx = make_ctx(instance_id="cs_replies", template=TEMPLATE.name, tools=registry)

    parked = run_once(ctx, TEMPLATE)
    store.decide_approval(parked.approval_id, ApprovalState.APPROVED.value, "owner", "ok")

    # The resume sends exactly what the human read: the payload is in the
    # checkpoint, so a resume can never silently substitute a different one.
    assert run_once(ctx, TEMPLATE).status is LoopStatus.COMPLETED
    assert send.count == 1
    assert send.calls[0]["draft"]["body"] == "on its way"

    revised = run_once(ctx, TEMPLATE)
    assert revised.status is LoopStatus.PARKED
    assert revised.approval_id != parked.approval_id
    assert send.count == 1, "the revised text must not ride the earlier approval"


def test_approval_requested_event_is_written_for_the_dashboard(make_ctx, store):
    ctx = _ctx(make_ctx, Counter())
    run_once(ctx, TEMPLATE)
    kinds = {row["type"] for row in store.get_events_after(0, None, 500)}
    assert "approval.requested" in kinds
    assert "loop.approval" in kinds
    assert "loop.node" in kinds


def test_nothing_to_send_is_idle_neither_a_failure_nor_a_success(make_ctx):
    send = Counter()
    ctx = _ctx(make_ctx, send, draft={})
    report = run_once(ctx, TEMPLATE)
    assert report.status is LoopStatus.IDLE
    # Not a failure — that would pause a healthy loop on a quiet day. Not an
    # acceptance either — that is what let a loop with nothing to do report
    # 100% forever. It is excluded from the denominator entirely.
    assert report.as_dict()["accepted"] is False
    assert report.as_dict()["outcome"] == "neutral"
    assert send.count == 0


def test_expiry_window_comes_from_policy(make_ctx, store):
    ctx = _ctx(make_ctx, Counter())
    run_once(ctx, TEMPLATE)
    row = _rows(store)[0]
    assert row["expires_at"] > utc_now_iso()


# --------------------------------------------------------------------------
# what the approvals page shows a human must not be a credential
# --------------------------------------------------------------------------


def test_nested_credentials_are_redacted_not_just_top_level_keys():
    """``params_json`` is rendered to whoever opens the approvals page.

    The predecessor scanned only TOP-LEVEL keys and stringified everything else,
    so ``{"headers": {"Authorization": "Bearer …"}}`` — the single most common
    way a credential appears in a connector call — was written out verbatim.
    Redaction is recursive now, and matches key NAMES at every depth.
    """
    from omniagentos_loops.approvals import _redacted

    out = _redacted(
        {
            "headers": {"Authorization": "Bearer sk-live-DEADBEEFDEADBEEF"},
            "auth": {"nested": {"api_key": "xoxb-1111111-secret"}},
            "batch": [{"password": "hunter2"}, {"note": "fine"}],
            "url": "https://example.com/orders/42",
        }
    )
    flattened = json.dumps(out, sort_keys=True)
    for leaked in ("sk-live-DEADBEEFDEADBEEF", "xoxb-1111111-secret", "hunter2"):
        assert leaked not in flattened, flattened
    assert out["headers"]["Authorization"] == "<redacted>"
    assert out["batch"][1]["note"] == "fine", "innocuous values must survive for review"
    assert out["url"] == "https://example.com/orders/42"


def test_credential_shaped_values_are_redacted_under_innocuous_keys():
    """The key that carries a token is not always called one."""
    from omniagentos_loops.approvals import _redacted

    out = _redacted(
        {
            "command": "curl -H 'Authorization: Bearer abcdef1234567890' https://api.example.com",
            "hook": "https://hooks.slack.com/services/T0/B0/zzzzzzzzzzzz",
            "note": {"deep": ["xoxb-999999999-abcdefgh"]},
            "count": 3,
        }
    )
    flattened = json.dumps(out, sort_keys=True)
    for leaked in ("abcdef1234567890", "zzzzzzzzzzzz", "xoxb-999999999-abcdefgh"):
        assert leaked not in flattened, flattened
    assert out["count"] == 3, "a scalar that is not a credential must stay readable"


def test_redaction_terminates_on_a_self_referential_payload():
    """A depth cap, not a visited-set: cheap, and it cannot be defeated by aliasing."""
    from omniagentos_loops.approvals import _redacted

    cycle: dict[str, Any] = {"token": "sekret"}
    cycle["self"] = cycle
    rendered = json.dumps(_redacted(cycle), sort_keys=True)
    assert "sekret" not in rendered
    assert "<depth>" in rendered


def test_the_redacted_arguments_are_what_the_row_stores(make_ctx, store):
    """End to end: the row an operator reads, not just the helper."""
    import json as _json

    ctx = _ctx(
        make_ctx,
        Counter(),
        draft={
            "to": "customer@example.com",
            "subject": "Re: order",
            "headers": {"Authorization": "Bearer sk-live-DEADBEEFDEADBEEF"},
            "body": "on its way",
        },
    )
    parked = run_once(ctx, TEMPLATE)
    row = next(r for r in _rows(store) if r["id"] == parked.approval_id)
    params = _json.loads(row["params_json"])
    assert "sk-live-DEADBEEFDEADBEEF" not in row["params_json"], params["args"]
    assert params["args"]["draft"]["headers"]["Authorization"] == "<redacted>"
    assert params["args"]["draft"]["body"] == "on its way"
