"""Verify plugin."""

from __future__ import annotations

PLUGIN_ID = "verify"
PRIORITY = 90


def handle_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"verify:{payload.get('name', '')}"
