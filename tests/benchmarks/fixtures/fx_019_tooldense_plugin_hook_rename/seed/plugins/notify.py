"""Notify plugin."""

from __future__ import annotations

PRIORITY = 70


def on_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"notify:{payload.get('name', '')}"
