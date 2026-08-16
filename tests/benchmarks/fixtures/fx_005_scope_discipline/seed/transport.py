"""Transport stub. Records the calls it received so checks can assert on them."""

from __future__ import annotations

from typing import Any

CALLS: list[dict[str, Any]] = []
FAIL_TIMES = 0


def send(path: str, *, timeout_s: float = 30.0) -> dict[str, Any]:
    CALLS.append({"path": path, "timeout_s": timeout_s})
    if len(CALLS) <= FAIL_TIMES:
        return {"ok": False, "path": path}
    return {"ok": True, "path": path, "timeout_s": timeout_s}
