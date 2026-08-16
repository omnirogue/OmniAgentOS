"""Purge plugin."""

from __future__ import annotations

PLUGIN_ID = "purge"
PRIORITY = 15


def handle_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"purge:{payload.get('name', '')}"
