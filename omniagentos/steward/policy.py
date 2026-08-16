"""Pure backlog-aging policy for Steward alerts and suggestions.

Deliberately isolated from any store/DB access: the sweep decision -- "has
this open case gone stale enough to auto-terminate" -- is a pure function of
rows already read into memory plus a caller-supplied ``now``, so it is
trivially unit tested without a database and cannot itself develop a hidden
I/O side effect. Callers (``omniagentos.steward.alerts.monitor``) own the
actual read + write against :class:`~omniagentos.steward.store.StewardStore`.

Council finding: 82 open alerts collapsing to ~3 cooldown keys and 71
suggestions collapsing to ~2 titles both came from nothing ever expiring.
Case identity (``store.create_alert`` / ``store.create_or_bump_suggestion``)
stops the pile-up for ACTIVE conditions; this policy is the safety net for
cases that just went quiet without ever explicitly resolving (a rule stopped
firing without a clean recovery signal being observable, or a suggestion
nobody ever triaged).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

ALERT_STALE_DAYS = 14
SUGGESTION_EXPIRE_DAYS = 14


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def stale_alert_ids(
    alerts: list[dict[str, Any]], *, now: datetime, stale_days: int = ALERT_STALE_DAYS
) -> list[int]:
    """Open alert ids idle (no new occurrence) for more than ``stale_days``.

    "Idle" is measured from the case's ``evidence["_case"]["last_seen"]``
    (refreshed on every occurrence -- see ``store.create_alert``), falling
    back to ``created_at`` for any open row that predates case-identity
    metadata. Non-open rows and rows with an unparsable/missing timestamp are
    left alone (never guessed at).
    """
    cutoff = now - timedelta(days=stale_days)
    stale: list[int] = []
    for alert in alerts:
        if alert.get("state") != "open":
            continue
        evidence = alert.get("evidence")
        case_meta = evidence.get("_case") if isinstance(evidence, dict) else None
        last_seen_raw = (case_meta or {}).get("last_seen") or alert.get("created_at")
        last_seen = _parse_iso(last_seen_raw)
        if last_seen is not None and last_seen < cutoff:
            stale.append(int(alert["id"]))
    return stale


def expired_suggestion_ids(
    suggestions: list[dict[str, Any]],
    *,
    now: datetime,
    expire_days: int = SUGGESTION_EXPIRE_DAYS,
) -> list[str]:
    """Open (undecided) suggestion ids older than ``expire_days`` since creation.

    Only ``state == 'open'`` rows are eligible: a suggestion already claimed
    (``approving``) or decided (any human terminal state) is never touched by
    the sweep.
    """
    cutoff = now - timedelta(days=expire_days)
    expired: list[str] = []
    for suggestion in suggestions:
        if suggestion.get("state") != "open":
            continue
        created_at = _parse_iso(suggestion.get("created_at"))
        if created_at is not None and created_at < cutoff:
            expired.append(str(suggestion["id"]))
    return expired


__all__ = [
    "ALERT_STALE_DAYS",
    "SUGGESTION_EXPIRE_DAYS",
    "expired_suggestion_ids",
    "stale_alert_ids",
]
