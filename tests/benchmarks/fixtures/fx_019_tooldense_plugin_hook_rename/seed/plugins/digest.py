"""Digest plugin."""

from __future__ import annotations

PRIORITY = 60


def on_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"digest:{payload.get('name', '')}"
