"""Digest plugin."""

from __future__ import annotations

PLUGIN_ID = "digest"
PRIORITY = 60


def handle_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"digest:{payload.get('name', '')}"
