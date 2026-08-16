"""Reference solution — the extracted helper."""

from __future__ import annotations

import json
from typing import Any


def format_frame(event: str, data: dict[str, Any]) -> str:
    """One SSE frame: an event name and its JSON payload."""
    body = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return f"event: {event}\ndata: {body}\n\n"
