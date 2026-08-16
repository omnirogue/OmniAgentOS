"""EDC adapter for the estate's single numbered Slack approval loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.edc.actions import draft_sha256, preview_send, run_executor
from omniagentos.edc.store import DecisionConflictError, DecisionStore
from omniagentos.team import decisions as team_decisions


class _Notifier(Protocol):
    def open_dm(self, slack_user_id: str) -> str | None: ...

    def post_dm(self, slack_user_id: str, text: str, **kwargs: Any) -> bool: ...


def register_edc_approval(
    repo_root: Path,
    store: DecisionStore,
    decision: dict[str, Any],
    *,
    notifier: _Notifier,
    owner_slack_id: str,
) -> dict[str, Any]:
    """Allocate the global number, persist DM channel, and send one proposal."""
    owner = str(decision["owner_employee_id"])
    draft = dict(decision.get("draft") or {})
    action_sha = draft_sha256(draft)
    if not action_sha or draft.get("sha256") != action_sha:
        raise ValueError("EDC approval requires a current draft sha256")
    channel = notifier.open_dm(owner_slack_id)
    if not channel:
        raise RuntimeError("could not open the owner's Slack DM")
    dedup_key = f"edc:{decision['id']}:{action_sha}"
    preview = preview_send(decision)
    with team_decisions._state_lock(repo_root):
        state = team_decisions.load_state(repo_root)
        team_decisions._expire(state)
        existing = next(
            (
                item
                for item in state["decisions"]
                if item.get("dedup_key") == dedup_key and item.get("status") == "pending"
            ),
            None,
        )
        if existing is not None:
            return existing
        number = int(state["next_number"])
        state["next_number"] = number + 1
        record = {
            "number": number,
            "kind": "edc",
            "dedup_key": dedup_key,
            "title": str(decision.get("title") or "Email reply"),
            "payload": {
                "decision_id": decision["id"],
                "owner_employee_id": owner,
                "owner_slack_id": owner_slack_id,
                "dm_channel": channel,
                "action_sha": action_sha,
            },
            "status": "pending",
            "proposed_at": team_decisions._iso(team_decisions._utcnow()),
            "decided_by": None,
            "decided_at": None,
            "result": None,
        }
        state["decisions"].append(record)
        try:
            store.await_approval(
                decision["id"],
                actor=owner,
                slack_number=number,
                dm_channel=channel,
                execution=preview,
            )
            team_decisions.save_state(repo_root, state)
        except BaseException:
            store.cancel_approval(
                decision["id"], actor=owner, note="Slack approval registration failed"
            )
            raise
    delivered = notifier.post_dm(
        owner_slack_id,
        f"📨 edc {number}. {decision.get('title') or 'Email reply'}\n"
        f"{preview['mime']}\n"
        f"Reply `edc {number} yes` / `edc {number} no`.",
    )
    if not delivered:
        with team_decisions._state_lock(repo_root):
            state = team_decisions.load_state(repo_root)
            for item in state["decisions"]:
                if item.get("dedup_key") == dedup_key and item.get("status") == "pending":
                    item["status"] = "failed"
                    item["result"] = "Slack DM delivery failed"
            team_decisions.save_state(repo_root, state)
        store.cancel_approval(decision["id"], actor=owner, note="Slack approval DM delivery failed")
        raise RuntimeError("could not deliver the owner's Slack approval DM")
    return record


def resolve_from_slack(
    record: dict[str, Any],
    *,
    approved: bool,
    actor: str,
    store: DecisionStore | None = None,
) -> dict[str, Any] | None:
    """Owner-only Slack reply -> the same SQLite CAS used by the dashboard."""
    payload = record.get("payload") or {}
    owner = str(payload.get("owner_employee_id") or "")
    if actor != owner:
        return None
    decisions = store or DecisionStore(SqliteStore(default_db_path()))
    decision = decisions.get_decision(
        str(payload.get("decision_id") or ""), owner_employee_id=owner
    )
    if decision is None:
        return None
    if not approved:
        try:
            return decisions.resolve(decision["id"], actor=actor, resolution="deny")
        except DecisionConflictError:
            return None
    draft = dict(decision.get("draft") or {})
    sha = draft_sha256(draft)
    if sha != payload.get("action_sha") or sha != draft.get("sha256"):
        return None
    draft.update({"approved_sha256": sha, "approved_at": utc_now_iso()})
    try:
        claimed = decisions.resolve(
            decision["id"],
            actor=actor,
            resolution="approve",
            params={"draft": draft, "expected_draft_sha256": sha},
        )
    except DecisionConflictError:
        return None
    return run_executor(decisions, claimed, actor=actor)


def expire_from_slack(
    record: dict[str, Any],
    *,
    store: DecisionStore | None = None,
    notifier: _Notifier | None = None,
) -> dict[str, Any] | None:
    """Mirror the JSON 24h expiry onto SQLite and re-escalate URGENT.

    Review F3: a decision that still holds a reply draft expires back to
    ``draft_pending`` (NOT ``open``) so the dashboard draft/send panel keeps
    rendering and the owner can approve from the dashboard after the Slack
    one-shot lapsed. A non-draft approval (execute/delegate/defer) expires to
    ``open`` as before.
    """
    payload = record.get("payload") or {}
    owner = str(payload.get("owner_employee_id") or "")
    decision_id = str(payload.get("decision_id") or "")
    decisions = store or DecisionStore(SqliteStore(default_db_path()))
    current = decisions.get_decision(decision_id, owner_employee_id=owner)
    has_draft = bool((current or {}).get("draft"))
    to_status = "draft_pending" if has_draft else "open"
    reopened = decisions.reopen(
        decision_id,
        owner_employee_id=owner,
        from_status="awaiting_approval",
        event="expire",
        note="Slack approval expired after 24h",
        to_status=to_status,
    )
    if reopened and reopened.get("classification") == "urgent" and notifier is not None:
        notifier.post_dm(
            str(payload.get("owner_slack_id") or ""),
            f"🚨 EDC-{reopened['number']} approval expired; urgent review is still required.",
        )
    return reopened


__all__ = ["expire_from_slack", "register_edc_approval", "resolve_from_slack"]
