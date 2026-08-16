"""Archive plugin."""

from __future__ import annotations

PRIORITY = 40


def on_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"archive:{payload.get('name', '')}"
