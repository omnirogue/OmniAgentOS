"""Audit plugin."""

from __future__ import annotations

PLUGIN_ID = "audit"
PRIORITY = 30


def handle_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"audit:{payload.get('name', '')}"
