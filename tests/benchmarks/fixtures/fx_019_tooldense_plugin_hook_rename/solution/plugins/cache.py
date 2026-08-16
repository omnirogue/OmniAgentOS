"""Cache plugin."""

from __future__ import annotations

PLUGIN_ID = "cache"
PRIORITY = 20


def handle_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"cache:{payload.get('name', '')}"
