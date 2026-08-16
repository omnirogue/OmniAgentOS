"""Notify plugin."""

from __future__ import annotations

PLUGIN_ID = "notify"
PRIORITY = 70


def handle_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"notify:{payload.get('name', '')}"
