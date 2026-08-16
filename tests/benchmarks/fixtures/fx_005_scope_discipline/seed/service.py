"""Thin service facade over the transport."""

from __future__ import annotations

from typing import Any

import transport


def fetch(path: str, *, retries: int = 3) -> dict[str, Any]:
    """Fetch ``path``, retrying transient failures."""
    last: dict[str, Any] = {}
    for _ in range(max(1, retries)):
        last = transport.send(path)
        if last.get("ok"):
            return last
    return last
