"""Task lifecycle events."""

from __future__ import annotations

import json


def task_updated(task_id: str, status: str) -> str:
    payload = {"status": status, "task_id": task_id}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "event: task.updated\ndata: " + body + "\n\n"


def task_assigned(task_id: str, worker_id: str) -> str:
    payload = {"task_id": task_id, "worker_id": worker_id}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "event: task.updated\ndata: " + body + "\n\n"
