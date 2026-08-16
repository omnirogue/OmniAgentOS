"""Task lifecycle events."""

from __future__ import annotations

from emitters.frame import format_frame


def task_updated(task_id: str, status: str) -> str:
    return format_frame("task.updated", {"status": status, "task_id": task_id})


def task_assigned(task_id: str, worker_id: str) -> str:
    return format_frame("task.updated", {"task_id": task_id, "worker_id": worker_id})
