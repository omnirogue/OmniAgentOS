"""Alert events."""

from __future__ import annotations

import json


def alert_created(alert_id: str, severity: str) -> str:
    payload = {"alert_id": alert_id, "severity": severity}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "event: alert.created\ndata: " + body + "\n\n"
