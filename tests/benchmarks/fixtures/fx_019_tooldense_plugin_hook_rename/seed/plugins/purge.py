"""Purge plugin."""

from __future__ import annotations

PRIORITY = 15


def on_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"purge:{payload.get('name', '')}"
