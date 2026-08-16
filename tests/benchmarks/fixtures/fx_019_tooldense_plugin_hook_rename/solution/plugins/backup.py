"""Backup plugin."""

from __future__ import annotations

PLUGIN_ID = "backup"
PRIORITY = 50


def handle_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"backup:{payload.get('name', '')}"
