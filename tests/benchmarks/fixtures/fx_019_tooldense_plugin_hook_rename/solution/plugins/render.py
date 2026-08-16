"""Render plugin."""

from __future__ import annotations

PLUGIN_ID = "render"
PRIORITY = 45


def handle_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"render:{payload.get('name', '')}"
