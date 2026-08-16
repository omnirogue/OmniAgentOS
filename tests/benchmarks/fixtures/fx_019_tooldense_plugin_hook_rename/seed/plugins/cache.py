"""Cache plugin."""

from __future__ import annotations

PRIORITY = 20


def on_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"cache:{payload.get('name', '')}"
