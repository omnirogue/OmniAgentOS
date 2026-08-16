"""Render plugin."""

from __future__ import annotations

PRIORITY = 45


def on_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"render:{payload.get('name', '')}"
