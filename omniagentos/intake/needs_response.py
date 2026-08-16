"""Needs Response — conversations that cannot proceed until the operator acts.

§2 of HANDOFF/CONVERSATION-WORKSPACE.md. Define strictly:

  Needs Response = a board card whose session/run cannot proceed until the
  operator decides. That is: status is ``awaiting_approval``, or a pending
  approval is bound to the card (``pending_approval``), or the linked work
  state is ``awaiting_approval``.

Blocked cards (``status=blocked`` means FAILED) and open suggestions are
backlog — they stay on their existing surfaces and are NOT included. A naive
union is 104+; this predicate returns ~1 in production.

Ordering is the product (no client sort control):
  1. action_class risk (irreversible first)
  2. oldest waiting first
  3. board priority
  4. stable id
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# contracts.ActionClass ladder — lowest → highest risk. Higher rank sorts first.
_ACTION_CLASS_RANK: dict[str, int] = {
    "read_only": 0,
    "sandboxed_creation": 1,
    "internal_reversible": 2,
    "external_reversible": 3,
    "consequential": 4,
    "irreversible": 5,
}

_PRIORITY_RANK: dict[str, int] = {
    "urgent": 0,
    "high": 1,
    "normal": 2,
    "medium": 2,
    "low": 3,
}


# Terminal / backlog board statuses — never Needs Response, even if a stale
# pending approval row still hangs around after the card failed or was cancelled.
_TERMINAL_OR_BACKLOG_STATUS = frozenset({"blocked", "done", "cancelled", "pending"})


def is_needs_response(task: Mapping[str, Any]) -> bool:
    """True iff this conversation cannot proceed until the operator acts.

    Deliberately narrow: ``blocked`` (FAILED), ``done``, ``cancelled``, and
    unpromoted ``pending`` are NOT Needs Response — even when a stale
    ``pending_approval`` is still bound. Status, bound approval, and live work
    state are the three live signals; any one is enough while the card is live
    (status projection can lag a fresh approval).
    """
    status = str(task.get("status") or "")
    if status in _TERMINAL_OR_BACKLOG_STATUS:
        return False
    if status == "awaiting_approval":
        return True
    if task.get("pending_approval"):
        return True
    work = task.get("work")
    if isinstance(work, Mapping) and str(work.get("state") or "") == "awaiting_approval":
        return True
    if str(task.get("run_state") or "") == "awaiting_approval":
        return True
    return False


def _action_class_rank(task: Mapping[str, Any]) -> int:
    """Higher = riskier = sorts first. Unknown classes land in the middle."""
    approval = task.get("pending_approval")
    action_class = ""
    if isinstance(approval, Mapping):
        action_class = str(approval.get("action_class") or "")
    return _ACTION_CLASS_RANK.get(action_class, 2)


def _waiting_since(task: Mapping[str, Any]) -> str:
    """ISO timestamp of when the wait started — older first. Missing → far future."""
    approval = task.get("pending_approval")
    if isinstance(approval, Mapping):
        created = approval.get("created_at")
        if isinstance(created, str) and created.strip():
            return created
    updated = task.get("updated_at")
    if isinstance(updated, str) and updated.strip():
        return updated
    created = task.get("created_at")
    if isinstance(created, str) and created.strip():
        return created
    return "9999-12-31T23:59:59Z"


def _priority_rank(task: Mapping[str, Any]) -> int:
    return _PRIORITY_RANK.get(str(task.get("priority") or "normal").lower(), 2)


def needs_response_sort_key(task: Mapping[str, Any]) -> tuple[Any, ...]:
    """Sort key: riskier first, then oldest wait, then priority, then id."""
    return (
        -_action_class_rank(task),  # higher risk first
        _waiting_since(task),  # older first
        _priority_rank(task),
        str(task.get("id") or ""),
    )


def select_needs_response(tasks: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Filter to Needs Response and rank. Pure — no I/O.

    Input is the already-reconciled live board (same rows as GET /api/board).
    """
    selected = [dict(task) for task in tasks if is_needs_response(task)]
    selected.sort(key=needs_response_sort_key)
    return selected


def list_needs_response_payload(tasks: list[Mapping[str, Any]]) -> dict[str, Any]:
    """API payload: ranked items + count. Count is the definition-of-pass signal."""
    items = select_needs_response(tasks)
    return {"items": items, "count": len(items)}


__all__ = [
    "is_needs_response",
    "list_needs_response_payload",
    "needs_response_sort_key",
    "select_needs_response",
]
