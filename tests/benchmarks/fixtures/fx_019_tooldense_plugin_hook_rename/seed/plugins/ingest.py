"""Ingest plugin."""

from __future__ import annotations

PRIORITY = 80


def on_event(payload: dict[str, str]) -> str:
    """Handle one event."""
    return f"ingest:{payload.get('name', '')}"
