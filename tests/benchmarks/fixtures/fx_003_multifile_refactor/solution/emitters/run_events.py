"""Run lifecycle events."""

from __future__ import annotations

from emitters.frame import format_frame


def run_updated(run_id: str, state: str) -> str:
    return format_frame("run.updated", {"run_id": run_id, "state": state})


def run_failed(run_id: str, error: str) -> str:
    return format_frame("run.updated", {"error": error, "run_id": run_id, "state": "failed"})
