"""Backup plugin."""

from __future__ import annotations

PRIORITY = 50


def on_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"backup:{payload.get('name', '')}"
