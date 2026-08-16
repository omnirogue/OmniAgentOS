"""Alert events."""

from __future__ import annotations

from emitters.frame import format_frame


def alert_created(alert_id: str, severity: str) -> str:
    return format_frame("alert.created", {"alert_id": alert_id, "severity": severity})
