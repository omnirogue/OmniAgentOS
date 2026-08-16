"""Archive plugin."""

from __future__ import annotations

PLUGIN_ID = "archive"
PRIORITY = 40


def handle_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"archive:{payload.get('name', '')}"
