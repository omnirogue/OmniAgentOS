"""Export plugin."""

from __future__ import annotations

PRIORITY = 10


def on_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"export:{payload.get('name', '')}"
