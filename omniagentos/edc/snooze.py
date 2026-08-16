"""Deadline-aware EDC snooze validation and server suggestions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from omniagentos.edc.store import DecisionStore
from omniagentos.team.tasks import parse_deadline


class _Notifier(Protocol):
    def post_dm(
        self,
        slack_user_id: str,
        text: str,
        *,
        blocks: list | None = None,
        color: str | None = None,
    ) -> bool: ...


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def suggested_snoozes(
    decision: dict[str, Any], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    deadline = _parse(decision.get("deadline_at"))
    choices = [
        ("Tomorrow", now + timedelta(days=1)),
        ("In 3 days", now + timedelta(days=3)),
    ]
    if deadline is not None:
        choices.append(("24h before deadline", deadline - timedelta(hours=24)))
    return [
        {
            "label": label,
            "until": until.isoformat(),
            "past_deadline": deadline is not None and until > deadline - timedelta(hours=24),
        }
        for label, until in choices
        if until > now
    ]


def resolve_snooze(
    store: DecisionStore,
    decision: dict[str, Any],
    *,
    actor: str,
    until: str,
    acknowledge_deadline: bool = False,
    note: str | None = None,
    tags: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    target = _parse(until)
    persisted_until = until.strip()
    if target is None:
        parsed = parse_deadline(until, now=now)
        target = _parse(parsed)
        persisted_until = str(parsed or "")
    if target is None:
        raise ValueError("snooze time is not a supported ISO timestamp or deadline phrase")
    if target <= now:
        raise ValueError("snooze time must be in the future")
    deadline = _parse(decision.get("deadline_at"))
    acknowledgement = ""
    if deadline is not None and target > deadline - timedelta(hours=24):
        if not acknowledge_deadline:
            raise ValueError(
                f"snooze crosses the 24h safety window for deadline {deadline.isoformat()}; "
                "set acknowledge_deadline=true to confirm that deadline"
            )
        acknowledgement = f"acknowledged deadline {deadline.isoformat()}"
    audit_note = "\n".join(part for part in ((note or "").strip(), acknowledgement) if part)
    return store.resolve(
        decision["id"],
        actor=actor,
        resolution="snooze",
        note=audit_note,
        tags=tags,
        params={"snooze_until": persisted_until},
    )


def sweep_snoozes(
    store: DecisionStore,
    owner_employee_ids: list[str],
    *,
    now: datetime | None = None,
    notifier: _Notifier | None = None,
    slack_reverse: dict[str, str] | None = None,
) -> dict[str, int]:
    """Resurface elapsed snoozes and one-shot DM deadlines inside 24 hours."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    stats = {"resurfaced": 0, "deadline_escalated": 0, "dm_failed": 0}
    for owner in owner_employee_ids:
        rows = store.list_decisions(owner_employee_id=owner, status="snoozed")
        for row in rows:
            snooze_until = _parse(row.get("snooze_until"))
            current = row
            if snooze_until is not None and snooze_until <= moment:
                reopened = store.reopen(
                    row["id"],
                    owner_employee_id=owner,
                    from_status="snoozed",
                    event="surface",
                    note=f"snooze elapsed at {snooze_until.isoformat()}",
                )
                if reopened is not None:
                    current = reopened
                    stats["resurfaced"] += 1

            deadline = _parse(current.get("deadline_at"))
            if (
                deadline is None
                or deadline > moment + timedelta(hours=24)
                or current.get("escalated_for_deadline")
            ):
                continue
            escalated = store.mark_deadline_escalated(current["id"], owner_employee_id=owner)
            if escalated is None:
                continue
            stats["deadline_escalated"] += 1
            slack_id = (slack_reverse or {}).get(owner)
            if (
                notifier is None
                or not slack_id
                or not notifier.post_dm(
                    slack_id,
                    f"🚨 EDC-{escalated['number']} deadline is within 24h "
                    f"({deadline.isoformat()}): {escalated['title']}",
                )
            ):
                stats["dm_failed"] += 1
    return stats


__all__ = ["resolve_snooze", "suggested_snoozes", "sweep_snoozes"]
