"""Audit plugin."""

from __future__ import annotations

PRIORITY = 30


def on_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"audit:{payload.get('name', '')}"
