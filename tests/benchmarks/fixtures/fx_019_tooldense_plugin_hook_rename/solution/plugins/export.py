"""Export plugin."""

from __future__ import annotations

PLUGIN_ID = "export"
PRIORITY = 10


def handle_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"export:{payload.get('name', '')}"
