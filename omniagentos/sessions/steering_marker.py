"""Filesystem signal for pending live-session steering."""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_VAR_ROOT = Path(__file__).resolve().parents[2] / "var"


def _safe_session_id(session_id: str) -> str:
    cleaned = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
    if not cleaned:
        raise ValueError("session_id must contain at least one safe character")
    return cleaned


def steering_marker_path(session_id: str) -> Path:
    """Return ``var/sessions/<session_id>/steering.pending``."""
    var_root = Path(os.environ.get("OMNIAGENTOS_VAR_DIR") or _DEFAULT_VAR_ROOT)
    return var_root / "sessions" / _safe_session_id(session_id) / "steering.pending"


def steering_pending(session_id: str) -> bool:
    """Stat the pending marker without reading or importing control-plane state."""
    try:
        steering_marker_path(session_id).stat()
    except (OSError, ValueError):
        return False
    return True


def mark_steering_pending(session_id: str) -> None:
    """Best-effort signal that a session has durable steering to claim."""
    try:
        path = steering_marker_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except (OSError, ValueError):
        # Steering remains durable in SQLite and is re-injected on resume.
        pass


def clear_steering_pending(session_id: str) -> None:
    """Best-effort clear after the session's steering queue is drained."""
    try:
        steering_marker_path(session_id).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


__all__ = [
    "clear_steering_pending",
    "mark_steering_pending",
    "steering_marker_path",
    "steering_pending",
]
