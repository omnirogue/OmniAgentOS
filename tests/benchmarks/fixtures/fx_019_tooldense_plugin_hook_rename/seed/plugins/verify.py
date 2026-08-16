"""Verify plugin."""

from __future__ import annotations

PRIORITY = 90


def on_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"verify:{payload.get('name', '')}"
