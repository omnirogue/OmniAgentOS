"""EDC P2: draft authority, send approval, recovery, races, and snoozes."""

from __future__ import annotations

import ast
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.edc import actions, approvals
from omniagentos.edc.actions import draft_sha256, execute_send, run_executor, safe_error
from omniagentos.edc.main import run_reconcile_sweep
from omniagentos.edc.reply import create_reply_draft, edit_reply_draft
from omniagentos.edc.snooze import resolve_snooze, suggested_snoozes, sweep_snoozes
from omniagentos.edc.store import (
    DecisionConflictError,
    DecisionOwnerError,
    DecisionStore,
    available_actions_for,
)
from omniagentos.grants import GrantsStore, is_grant_live
from tests.edc.conftest import make_decision

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


class _DraftClient:
    def complete_json(self, *_args: Any, **kwargs: Any) -> dict[str, str]:
        assert kwargs["purpose"] == "edc_draft_reply"
        assert kwargs["required_keys"] == ["subject", "body"]
        return {"subject": "Re: renewal", "body": "Thanks — approved on our side."}


class _FakeSendExecutor:
    consequential = True

    def __init__(self) -> None:
        self.sent = 0

    def preview(self, decision: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "send_email", "draft_sha256": decision["draft"]["sha256"]}

    def execute(
        self, decision: dict[str, Any], *, store: DecisionStore, actor: str
    ) -> dict[str, Any]:
        assert decision["draft"]["approved_sha256"] == decision["draft"]["sha256"]
        assert decision["owner_employee_id"] == actor
        self.sent += 1
        return {"provider_message_id": "gmail-1"}

    def verify(self, result: dict[str, Any]) -> dict[str, Any]:
        return {"verified": False, "provider_message_id": result["provider_message_id"]}


def _email_decision(store: DecisionStore, *, ref: str = "p2-1", **over: object) -> dict:
    payload = make_decision(
        source_ref=ref,
        source_account="gmail_ownera",
        counterparty="Customer <customer@example.com>",
        context="The customer asked whether we approve the renewal.",
        **over,
    )
    return store.create_decision(payload)[0]


def test_intent_draft_edit_voids_sha_then_explicit_approve_sends(
    decisions: DecisionStore,
    employees: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _email_decision(decisions)
    drafted = create_reply_draft(
        decisions,
        row,
        actor="emp_owner",
        intent="Approve the renewal and thank them",
        llm_client=_DraftClient(),
    )
    assert drafted["status"] == "draft_pending"
    original_sha = drafted["draft"]["sha256"]
    assert drafted["draft"]["approved_sha256"] is None

    previously_approved = dict(drafted["draft"])
    previously_approved["approved_sha256"] = original_sha
    decisions.update_decision(
        drafted["id"], owner_employee_id="emp_owner", fields={"draft": previously_approved}
    )
    edited = edit_reply_draft(
        decisions,
        decisions.get_decision(drafted["id"], owner_employee_id="emp_owner") or drafted,
        actor="emp_owner",
        subject="Re: renewal approved",
        body="Approved. Thank you.",
    )
    assert edited["draft"]["sha256"] != original_sha
    assert edited["draft"]["approved_sha256"] is None

    current_sha = edited["draft"]["sha256"]
    approved_draft = dict(edited["draft"])
    approved_draft["approved_sha256"] = current_sha
    claimed = decisions.resolve(
        edited["id"],
        actor="emp_owner",
        resolution="approve",
        params={"draft": approved_draft, "expected_draft_sha256": current_sha},
    )
    fake = _FakeSendExecutor()
    monkeypatch.setitem(actions.EXECUTORS, "send_email", fake)
    sent = run_executor(decisions, claimed, actor="emp_owner")
    assert fake.sent == 1
    assert sent["status"] == "done_unverified"
    assert [
        event["event"] for event in decisions.list_events(sent["id"], owner_employee_id="emp_owner")
    ] == ["create", "draft", "edit", "approve", "send"]


def test_resolve_refuses_even_operator_for_another_owners_queue(
    decisions: DecisionStore, employees: dict[str, str]
) -> None:
    other = _email_decision(decisions, ref="p2-owner", owner_employee_id="emp_bob")
    with pytest.raises(DecisionOwnerError):
        decisions.resolve(other["id"], actor="emp_owner", resolution="dismiss")
    current = decisions.get_decision(other["id"], owner_employee_id="emp_bob")
    assert current is not None and current["status"] == "open"


def test_execute_send_requires_approval_and_mints_real_broker_grant(
    decisions: DecisionStore,
    employees: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _email_decision(decisions, ref="p2-grant")
    draft = {"to": "customer@example.com", "subject": "Hello", "body": "Approved."}
    draft["sha256"] = draft_sha256(draft)
    row["draft"] = dict(draft)
    called: list[dict[str, Any]] = []

    def fake_call(capability: str, granted: list[str], **kwargs: Any) -> dict[str, Any]:
        called.append({"capability": capability, "granted": granted, **kwargs})
        return {"ok": True, "status": 200, "body": {"id": "gmail-provider-id"}}

    monkeypatch.setattr("omniagentos.connectors.broker.call", fake_call)
    with pytest.raises(PermissionError, match="not approved"):
        execute_send(row, store=decisions, actor="emp_owner")
    assert called == []

    row["draft"]["approved_sha256"] = row["draft"]["sha256"]
    result = execute_send(row, store=decisions, actor="emp_owner")
    assert result["provider_message_id"] == "gmail-provider-id"
    assert len(called) == 1
    call = called[0]
    assert call["capability"] == "gmail_ownera.send"
    assert call["approval_token"].startswith("gnt_")
    assert call.get("dry_run") is None
    grant = GrantsStore(decisions._store).get_grant(call["approval_token"])
    assert grant is not None
    assert grant["plan_approval_state"] == "approved"
    assert grant["max_actions"] == 1
    assert grant["target_set"] == ["customer@example.com"]
    assert is_grant_live(grant, capability="gmail_ownera.send")[0] is True


def test_slack_approval_persists_dm_channel_and_expiry_reopens_urgent(
    decisions: DecisionStore,
    employees: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path / "var"))
    row = _email_decision(
        decisions, ref="p2-register", status="draft_pending", classification="urgent"
    )
    draft = {"to": "customer@example.com", "subject": "Hi", "body": "Body"}
    draft["sha256"] = draft_sha256(draft)
    row = decisions.update_decision(row["id"], owner_employee_id="emp_owner", fields={"draft": draft})
    assert row is not None

    class Notifier:
        def __init__(self) -> None:
            self.posts: list[str] = []

        def open_dm(self, slack_user_id: str) -> str | None:
            assert slack_user_id == "UOWNER"
            return "DOWNER"

        def post_dm(self, slack_user_id: str, text: str, **_kwargs: Any) -> bool:
            self.posts.append(f"{slack_user_id}:{text}")
            return True

    notifier = Notifier()
    record = approvals.register_edc_approval(
        tmp_path,
        decisions,
        row,
        notifier=notifier,
        owner_slack_id="UOWNER",
    )
    awaiting = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert awaiting is not None
    assert awaiting["status"] == "awaiting_approval"
    assert awaiting["dm_channel"] == "DOWNER"
    assert awaiting["slack_number"] == record["number"]
    reopened = approvals.expire_from_slack(record, store=decisions, notifier=notifier)
    # Review F3: this decision holds a reply draft, so expiry returns it to
    # draft_pending (NOT open) — the dashboard draft/send panel keeps rendering
    # and the owner can approve from the dashboard after the Slack one-shot lapsed.
    assert reopened is not None and reopened["status"] == "draft_pending"
    assert "approve" in reopened["available_actions"]
    assert any("approval expired" in post for post in notifier.posts)


def test_expiry_without_a_draft_reopens_to_open(
    decisions: DecisionStore,
    employees: dict[str, str],
) -> None:
    """A non-draft approval (execute/delegate/defer) still expires to open (F3)."""
    row = _email_decision(
        decisions, ref="p2-nodraft", status="awaiting_approval", classification="needs_owner"
    )
    record = {
        "payload": {
            "decision_id": row["id"],
            "owner_employee_id": "emp_owner",
            "owner_slack_id": "UOWNER",
        }
    }
    reopened = approvals.expire_from_slack(record, store=decisions)
    assert reopened is not None and reopened["status"] == "open"


def test_only_execute_send_imports_broker_on_edc_send_path() -> None:
    root = Path(__file__).resolve().parents[2] / "omniagentos" / "edc"
    hits: list[tuple[str, str | None]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self, filename: str) -> None:
            self.filename = filename
            self.functions: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module == "omniagentos.connectors" and any(
                alias.name == "broker" for alias in node.names
            ):
                hits.append((self.filename, self.functions[-1] if self.functions else None))

    for path in sorted(root.glob("*.py")):
        Visitor(path.name).visit(ast.parse(path.read_text(encoding="utf-8")))
    assert hits == [("actions.py", "execute_send")]


def test_executor_failure_transitions_to_failed_retryable_never_done(
    decisions: DecisionStore,
    employees: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _email_decision(decisions, ref="p2-crash", status="draft_pending")
    draft = {"to": "customer@example.com", "subject": "Hi", "body": "Body"}
    draft["sha256"] = draft_sha256(draft)
    draft["approved_sha256"] = draft["sha256"]
    decisions.update_decision(row["id"], owner_employee_id="emp_owner", fields={"draft": draft})
    claimed = decisions.resolve(
        row["id"],
        actor="emp_owner",
        resolution="approve",
        params={"draft": draft, "expected_draft_sha256": draft["sha256"]},
    )

    class CrashExecutor(_FakeSendExecutor):
        def execute(
            self, decision: dict[str, Any], *, store: DecisionStore, actor: str
        ) -> dict[str, Any]:
            raise TimeoutError("simulated pre-dispatch crash")

    monkeypatch.setitem(actions.EXECUTORS, "send_email", CrashExecutor())
    with pytest.raises(TimeoutError, match="simulated"):
        run_executor(decisions, claimed, actor="emp_owner")
    failed = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert failed is not None
    assert failed["status"] == "failed_retryable"
    assert failed["status"] != "done_unverified"


def test_dashboard_and_slack_approval_race_has_one_cas_winner(
    decisions: DecisionStore,
    employees: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _email_decision(decisions, ref="p2-race", status="draft_pending")
    draft = {"to": "customer@example.com", "subject": "Hi", "body": "Body"}
    draft["sha256"] = draft_sha256(draft)
    decisions.update_decision(row["id"], owner_employee_id="emp_owner", fields={"draft": draft})
    record = {
        "payload": {
            "decision_id": row["id"],
            "owner_employee_id": "emp_owner",
            "action_sha": draft["sha256"],
        }
    }
    monkeypatch.setattr(approvals, "run_executor", lambda _s, claimed, *, actor: claimed)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def dashboard() -> None:
        approved = dict(draft)
        approved["approved_sha256"] = draft["sha256"]
        barrier.wait()
        try:
            decisions.resolve(
                row["id"],
                actor="emp_owner",
                resolution="approve",
                params={
                    "draft": approved,
                    "expected_draft_sha256": draft["sha256"],
                },
            )
            outcomes.append("dashboard")
        except DecisionConflictError:
            outcomes.append("dashboard-409")

    def slack() -> None:
        barrier.wait()
        result = approvals.resolve_from_slack(
            record, approved=True, actor="emp_owner", store=decisions
        )
        outcomes.append("slack" if result is not None else "slack-409")

    threads = [threading.Thread(target=dashboard), threading.Thread(target=slack)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert sum(item in {"dashboard", "slack"} for item in outcomes) == 1
    assert sum(item.endswith("409") for item in outcomes) == 1


def test_snooze_explicit_suggested_deadline_guard_and_resurface(
    decisions: DecisionStore,
    employees: dict[str, str],
) -> None:
    deadline = NOW + timedelta(days=4)
    row = _email_decision(decisions, ref="p2-snooze", deadline_at=deadline.isoformat())
    suggestions = suggested_snoozes(row, now=NOW)
    assert [choice["label"] for choice in suggestions] == [
        "Tomorrow",
        "In 3 days",
        "24h before deadline",
    ]
    snoozed = resolve_snooze(
        decisions,
        row,
        actor="emp_owner",
        until="tomorrow",
        now=NOW,
    )
    assert snoozed["status"] == "snoozed"

    guarded = _email_decision(
        decisions, ref="p2-snooze-guard", deadline_at=(NOW + timedelta(hours=4)).isoformat()
    )
    with pytest.raises(ValueError, match="acknowledge_deadline=true"):
        resolve_snooze(
            decisions,
            guarded,
            actor="emp_owner",
            until=(NOW + timedelta(hours=5)).isoformat(),
            now=NOW,
        )
    accepted = resolve_snooze(
        decisions,
        guarded,
        actor="emp_owner",
        until=(NOW + timedelta(hours=1)).isoformat(),
        acknowledge_deadline=True,
        note="customer requested later",
        now=NOW,
    )
    event = decisions.list_events(accepted["id"], owner_employee_id="emp_owner")[-1]
    assert (NOW + timedelta(hours=4)).isoformat() in event["note"]

    class Notifier:
        def __init__(self) -> None:
            self.posts: list[str] = []

        def post_dm(self, slack_user_id: str, text: str, **_kwargs: Any) -> bool:
            self.posts.append(f"{slack_user_id}:{text}")
            return True

    notifier = Notifier()
    stats = sweep_snoozes(
        decisions,
        ["emp_owner"],
        now=NOW + timedelta(hours=2),
        notifier=notifier,
        slack_reverse={"emp_owner": "UOWNER"},
    )
    assert stats["resurfaced"] == 1
    assert stats["deadline_escalated"] == 1
    resurfaced = decisions.get_decision(accepted["id"], owner_employee_id="emp_owner")
    assert resurfaced is not None and resurfaced["status"] == "open"
    assert notifier.posts
    assert (NOW + timedelta(hours=4)).isoformat() in notifier.posts[0]


# --- Review F1: recovery states are RESOLVABLE, never frozen -----------------


def test_failed_retryable_can_be_edited_reapproved_and_resent(
    decisions: DecisionStore,
    employees: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transiently-failed send is recoverable: edit → re-approve → re-send."""
    row = _email_decision(decisions, ref="p2-failretry", status="failed_retryable")
    assert available_actions_for(row) == ["approve", "edit", "dismiss", "note"]

    new_draft = {"to": "customer@example.com", "subject": "Re", "body": "retry body"}
    new_draft["sha256"] = draft_sha256(new_draft)
    edited = decisions.resolve(
        row["id"], actor="emp_owner", resolution="edit", params={"draft": new_draft}
    )
    assert edited["status"] == "draft_pending"

    approved = dict(new_draft)
    approved["approved_sha256"] = new_draft["sha256"]
    claimed = decisions.resolve(
        edited["id"],
        actor="emp_owner",
        resolution="approve",
        params={"draft": approved, "expected_draft_sha256": new_draft["sha256"]},
    )
    assert claimed["status"] == "in_progress"

    fake = _FakeSendExecutor()
    monkeypatch.setitem(actions.EXECUTORS, "send_email", fake)
    sent = run_executor(decisions, claimed, actor="emp_owner")
    assert fake.sent == 1
    assert sent["status"] == "done_unverified"


def test_reconcile_required_can_be_dismissed_but_never_auto_resent(
    decisions: DecisionStore,
    employees: dict[str, str],
) -> None:
    """The ambiguous-crash state is human-only: dismissable, never re-sent."""
    row = _email_decision(decisions, ref="p2-reconcile", status="reconcile_required")
    # No automated re-send affordance at all.
    assert available_actions_for(row) == ["dismiss", "note"]
    assert "approve" not in available_actions_for(row)
    assert "execute" not in available_actions_for(row)
    with pytest.raises(ValueError, match="unavailable"):
        decisions.resolve(
            row["id"],
            actor="emp_owner",
            resolution="approve",
            params={"expected_draft_sha256": "x"},
        )
    # But a human can dismiss it after checking the provider.
    dismissed = decisions.resolve(row["id"], actor="emp_owner", resolution="dismiss")
    assert dismissed["status"] == "dismissed"


# --- Review F2: no raw exception message may egress --------------------------


def test_executor_failure_never_egresses_the_exception_message(
    decisions: DecisionStore,
    employees: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret-shaped exception string must not reach the persisted execution."""
    row = _email_decision(decisions, ref="p2-secret", status="draft_pending")
    draft = {"to": "customer@example.com", "subject": "Hi", "body": "Body"}
    draft["sha256"] = draft_sha256(draft)
    draft["approved_sha256"] = draft["sha256"]
    decisions.update_decision(row["id"], owner_employee_id="emp_owner", fields={"draft": draft})
    claimed = decisions.resolve(
        row["id"],
        actor="emp_owner",
        resolution="approve",
        params={"draft": draft, "expected_draft_sha256": draft["sha256"]},
    )

    secret = "sk-live-DEADBEEFsupersecrettoken"

    class LeakyExecutor(_FakeSendExecutor):
        def execute(
            self, decision: dict[str, Any], *, store: DecisionStore, actor: str
        ) -> dict[str, Any]:
            raise TimeoutError(f"gmail refused with credential {secret}")

    monkeypatch.setitem(actions.EXECUTORS, "send_email", LeakyExecutor())
    with pytest.raises(TimeoutError):
        run_executor(decisions, claimed, actor="emp_owner")

    failed = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert failed is not None
    assert failed["status"] == "failed_retryable"  # transient → recoverable
    # F2: the persisted execution error is a type label, never the raw message.
    assert secret not in json.dumps(failed["execution"])
    assert failed["execution"]["error"] == "TimeoutError"


def test_safe_error_emits_type_and_reason_never_the_message() -> None:
    class _Denied(PermissionError):
        def __init__(self) -> None:
            super().__init__("leaked sk-live-SECRET in the message body")
            self.reason = "grant_exhausted"

    label = safe_error(_Denied())
    assert "sk-live-SECRET" not in label
    assert label.endswith(":grant_exhausted")
    assert safe_error(ValueError("another sk-live-SECRET")) == "ValueError"


# --- Cheap improvement: stale in_progress → reconcile_required (never re-sends)


def test_stale_in_progress_routes_to_reconcile_required(
    decisions: DecisionStore,
    employees: dict[str, str],
) -> None:
    row = _email_decision(decisions, ref="p2-stale", status="in_progress")
    # Sweep with a clock well past the staleness threshold; the row's real
    # updated_at (creation) is older than the cutoff.
    stats = run_reconcile_sweep(
        decisions._store, ["emp_owner"], now=datetime.now(UTC) + timedelta(minutes=30)
    )
    assert stats["reconcile_routed"] == 1
    routed = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert routed is not None and routed["status"] == "reconcile_required"
    # It is NEVER auto-resent: no approve/execute affordance.
    assert "approve" not in available_actions_for(routed)


def test_fresh_in_progress_is_not_swept(
    decisions: DecisionStore,
    employees: dict[str, str],
) -> None:
    row = _email_decision(decisions, ref="p2-fresh", status="in_progress")
    stats = run_reconcile_sweep(decisions._store, ["emp_owner"])
    assert stats["reconcile_routed"] == 0
    fresh = decisions.get_decision(row["id"], owner_employee_id="emp_owner")
    assert fresh is not None and fresh["status"] == "in_progress"
