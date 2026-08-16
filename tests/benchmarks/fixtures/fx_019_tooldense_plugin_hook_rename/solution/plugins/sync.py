"""Sync plugin."""

from __future__ import annotations

PLUGIN_ID = "sync"
PRIORITY = 85


def handle_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"sync:{payload.get('name', '')}"
