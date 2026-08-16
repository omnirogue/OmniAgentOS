"""Reference solution."""

from __future__ import annotations

from typing import Any

import transport


def fetch(path: str, *, retries: int = 3, timeout_s: float = 5.0) -> dict[str, Any]:
    """Fetch ``path``, retrying transient failures."""
    last: dict[str, Any] = {}
    for _ in range(max(1, retries)):
        last = transport.send(path, timeout_s=timeout_s)
        if last.get("ok"):
            return last
    return last
