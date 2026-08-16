"""T-24h re-push for pending approvals that are about to expire.

An approval notification fires ONCE, when the approval is created. If the
operator misses that banner, nothing ever mentions it again -- the approval sits
pending until ``expires_at`` passes and the gated work is silently voided. This
module closes that gap: a pending approval whose expiry is inside the next 24
hours earns exactly ONE reminder, through the same
:func:`~omniagentos.notifications.service.record_notification` write seam every
other source uses.

**Why a distinct ref_type, not a re-fire of the original.** The write seam
dedupes on ``(ref_type, ref_id)`` while an unread row exists -- which is exactly
the situation a reminder is FOR (the operator has not read the original). A
plain re-fire would therefore be swallowed as a duplicate, and worse, the seam
still pushes its banner on the deduped call, so the row count and the banner
would disagree. The reminder is written against ``ref_type='approval_reminder'``
with the SAME approval id, so:

- it can never collide with the original approval row's dedupe key;
- :func:`~omniagentos.notifications.service.resolve_target` still resolves it
  through the approval branch (same ``approval_id``, same ``/approvals`` href,
  same live pending/approved/rejected state), so the reminder is actionable in
  the panel rather than a dead informational row; and
- its ``kind`` stays ``approval``, which is in both ``PUSH_KINDS`` and
  ``ACTIONABLE_KINDS`` -- the reminder buzzes the device and counts in the
  actionable badge, which is the entire point of sending it.

(``kind='approval_reminder'`` would have been the more literal encoding, but the
``notifications.kind`` CHECK constraint is fixed by migration 050 and migrations
are append-only + out of scope here; the ref_type carries the distinction with
the same effect and no schema change.)

**Once and only once** is enforced read-agnostically via
:meth:`NotificationsDal.has_for_ref_kind` on that distinct ref -- mirroring
``notify_task_done``. A reminder the operator has already READ must not be
re-sent on the next tick, so an unread-only guard would be wrong here.

**And it is enforced ATOMICALLY.** The guard read and the insert used to be two
separate transactions, which is a textbook time-of-check/time-of-use hole: two
sweeps overlapping (the steward monitor's cycle re-entered, a manual sweep, a
restarted host racing the running one) both read "no reminder yet" and both
insert, so the operator is reminded twice about the same approval -- the exact
noise this rail exists to avoid. The check now runs as the ``persistence_guard``
of :func:`~omniagentos.notifications.service.record_notification_result`, which
the DAL evaluates AFTER ``BEGIN IMMEDIATE`` and keeps locked through the insert
and commit. The second sweep therefore blocks on the write lock, and when it is
let through its guard observes the committed row and declines.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from omniagentos.notifications.dal import NotificationsDal
from omniagentos.notifications.service import record_notification_result

logger = logging.getLogger(__name__)

#: ``ref_type`` the reminder is written against (see the module docstring).
REMINDER_REF_TYPE = "approval_reminder"
#: How close to expiry an approval must be before it earns its reminder.
REMINDER_WINDOW_HOURS = 24
#: Upper bound on pending approvals inspected per cycle (cheap, deterministic).
DEFAULT_APPROVAL_LIMIT = 500

Summary = dict[str, int]


class _AlreadyReminded(RuntimeError):
    """Raised INSIDE the write transaction when this approval already has one.

    Not an error condition: it is how a guard declines. The DAL wraps whatever a
    guard raises and :func:`record_notification_result` re-raises the original,
    so this sentinel is how "already reminded" travels back out of the
    transaction that proved it -- rolling the would-be insert back on the way.
    A distinct type keeps it from being confused with a real write failure.
    """


def _already_reminded_guard(
    dal: NotificationsDal, approval_id: str
) -> Callable[[sqlite3.Connection], None]:
    """Build the transactional once-only guard for one approval.

    The returned callable is handed the connection that already holds
    ``BEGIN IMMEDIATE``; it consults ``dal.has_for_ref_kind``, which reads
    through that same connection (and therefore that same transaction), so the
    predicate has exactly ONE definition and still sees a consistent snapshot.
    """

    def guard(_connection: sqlite3.Connection) -> None:
        if dal.has_for_ref_kind(REMINDER_REF_TYPE, approval_id, "approval"):
            raise _AlreadyReminded(approval_id)

    return guard


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a stored ISO-8601 timestamp as UTC, or None when unusable.

    An unparseable/absent ``expires_at`` yields None, and the caller SKIPS that
    approval: an unreadable expiry is not evidence that expiry is imminent, and
    inventing one would spam a reminder for every malformed row on every tick.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def is_expiring_soon(
    expires_at: Any,
    now: datetime,
    *,
    window_hours: int = REMINDER_WINDOW_HOURS,
) -> bool:
    """Whether ``expires_at`` lands inside ``(now, now + window]``.

    Already-past expiry is deliberately EXCLUDED: a lapsed approval is not a
    "you have 24 hours" reminder, and pending rows whose expiry passed long ago
    (never swept) would otherwise each earn a pointless bell.
    """
    deadline = _parse_timestamp(expires_at)
    if deadline is None:
        return False
    return now < deadline <= now + timedelta(hours=window_hours)


def _reminder_body(approval: dict[str, Any], deadline: datetime | None) -> str:
    action = str(approval.get("proposed_action") or "").strip()
    action_class = str(approval.get("action_class") or "").strip()
    detail = f"{action_class}: {action}".strip(": ").strip()
    when = deadline.strftime("%Y-%m-%dT%H:%M:%SZ") if deadline is not None else "soon"
    lead = f"Expires {when} and is still waiting on you."
    return f"{lead} {detail}".strip()


def remind_expiring_approvals(
    approvals: list[dict[str, Any]],
    *,
    dal: NotificationsDal,
    now: datetime | None = None,
    window_hours: int = REMINDER_WINDOW_HOURS,
) -> Summary:
    """Emit at most one reminder per about-to-expire pending approval.

    ``approvals`` are rows straight from ``approvals`` (the caller decides how
    they are read); only ``state='pending'`` rows are considered, so passing a
    wider list is safe. Returns deterministic counts:
    ``{"considered", "reminded", "already_reminded", "failed"}``.

    Each approval is handled independently — one malformed row or one failed
    write must not stop the remaining reminders (the same isolation contract the
    alert monitor applies to its rules).
    """
    current = now or datetime.now(UTC)
    summary: Summary = {"considered": 0, "reminded": 0, "already_reminded": 0, "failed": 0}
    for approval in approvals:
        approval_id = str(approval.get("id") or "").strip()
        if not approval_id:
            continue
        if str(approval.get("state") or "").strip().lower() != "pending":
            continue
        if not is_expiring_soon(approval.get("expires_at"), current, window_hours=window_hours):
            continue
        summary["considered"] += 1
        try:
            deadline = _parse_timestamp(approval.get("expires_at"))
            result = record_notification_result(
                kind="approval",
                title="Approval expiring soon",
                body=_reminder_body(approval, deadline),
                severity="warning",
                ref_type=REMINDER_REF_TYPE,
                ref_id=approval_id,
                payload={
                    "approval_id": approval_id,
                    "reminder": True,
                    "expires_at": approval.get("expires_at"),
                    "action_class": approval.get("action_class"),
                    "proposed_action": approval.get("proposed_action"),
                    "session_id": approval.get("session_id"),
                    "run_id": approval.get("run_id"),
                },
                dal=dal,
                # Read-agnostic AND atomic: the guard runs inside the insert's
                # own BEGIN IMMEDIATE (see the module docstring), so a second
                # sweep cannot slip between the check and the write.
                persistence_guard=_already_reminded_guard(dal, approval_id),
                # The guard is the authoritative once-only check; the seam's
                # unread-only ref dedupe would add nothing and would re-open the
                # "already read → send again" hole.
                dedupe=False,
            )
            if result.status == "persisted":
                summary["reminded"] += 1
            else:
                # "deduped" here can only come from the seam's emission cap, and
                # a capped reminder was not delivered -- it is not a re-send.
                summary["failed"] += 1
        except _AlreadyReminded:
            summary["already_reminded"] += 1
        except Exception:  # noqa: BLE001 - one bad row must not blind the sweep
            logger.exception("approval reminder failed for %s", approval_id)
            summary["failed"] += 1
    return summary


def remind_from_store(
    database: Any,
    *,
    now: datetime | None = None,
    window_hours: int = REMINDER_WINDOW_HOURS,
    limit: int = DEFAULT_APPROVAL_LIMIT,
) -> Summary:
    """Run one reminder sweep against a ``SqliteStore``-shaped database.

    The notification is written through the store's OWN connection, so the
    reminder lands in the same database the approvals were read from (no
    split-brain with ``default_db_path()``).
    """
    approvals = database.list_approvals(state="pending", limit=limit)
    connection = getattr(database, "_connection", None)
    if connection is None:
        raise RuntimeError("database does not expose a connection to write notifications through")
    dal = NotificationsDal(connection)
    return remind_expiring_approvals(
        list(approvals),
        dal=dal,
        now=now,
        window_hours=window_hours,
    )


__all__ = [
    "DEFAULT_APPROVAL_LIMIT",
    "REMINDER_REF_TYPE",
    "REMINDER_WINDOW_HOURS",
    "is_expiring_soon",
    "remind_expiring_approvals",
    "remind_from_store",
]
