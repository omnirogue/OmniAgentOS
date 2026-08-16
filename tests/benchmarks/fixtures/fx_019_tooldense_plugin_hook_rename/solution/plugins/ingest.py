"""Ingest plugin."""

from __future__ import annotations

PLUGIN_ID = "ingest"
PRIORITY = 80


def handle_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"ingest:{payload.get('name', '')}"
