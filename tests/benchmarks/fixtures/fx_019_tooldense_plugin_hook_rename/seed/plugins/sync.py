"""Sync plugin."""

from __future__ import annotations

PRIORITY = 85


def on_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"sync:{payload.get('name', '')}"
