"""Run lifecycle events."""

from __future__ import annotations

import json


def run_updated(run_id: str, state: str) -> str:
    payload = {"run_id": run_id, "state": state}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "event: run.updated\ndata: " + body + "\n\n"


def run_failed(run_id: str, error: str) -> str:
    payload = {"error": error, "run_id": run_id, "state": "failed"}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "event: run.updated\ndata: " + body + "\n\n"
