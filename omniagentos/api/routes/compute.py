"""Estate Compute HTTP surface — one token-gated read over live compute facts.

  ``GET /api/compute/estate`` — the shared compute pool's queue/machines, the
                                 estate's GH Actions self-hosted runners, and
                                 this box's own load, in one envelope per
                                 source.

Read-side only: no store dependency, no writes. Same honesty envelope as
``omniagentos/testobs`` — a source that is absent, unreachable or unusable
answers ``{"available": false, "reason": ...}`` rather than zeros, so the
dashboard can style UNKNOWN as unknown instead of as success. See
``omniagentos/compute/readers.py`` for the collectors.

GATED (SEC-005): the handler carries ``_authorized``, exactly like testobs.
Pool machine identities/labels and runner names are the same class of
internal-work record as ``/api/testobs/*``, not a public status page. The
dashboard reaches it through ``proxyRead`` (the ``compute`` entry in
``AUTHORIZED_READ_PREFIXES``, ``dashboard/src/app/api/[...path]/route.ts``).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from omniagentos.api.routes.control import _authorized
from omniagentos.compute import read_local, read_pool, read_runners
from omniagentos.contracts import utc_now_iso

router = APIRouter(prefix="/api/compute", tags=["compute"])

_CACHE_TTL_S = 15.0
_COLLECTOR_UNAVAILABLE = "temporarily unavailable"

# Single slot — this endpoint takes no params, so there is exactly one thing
# to cache (unlike testobs's (category, days) key space). (started_at, value).
_cache: tuple[float, dict[str, Any]] | None = None
_cache_lock = threading.Lock()


def _all_available(snapshot: dict[str, Any]) -> bool:
    return all(bool(snapshot[source]["available"]) for source in ("pool", "runners", "local"))


def _safe_read(read: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Call one collector; a raise becomes its own absent envelope.

    Mirrors ``testobs.readers.snapshot``'s ``except Exception`` boundary: a
    reader that raises at all reports unavailable — one broken source must
    never take the other two, or the whole endpoint, down with it.
    """
    try:
        return read()
    except Exception:  # noqa: BLE001 — degrade this one source, never 500 the route
        return {"available": False, "reason": _COLLECTOR_UNAVAILABLE}


def snapshot(*, refresh: bool = False) -> dict[str, Any]:
    """TTL-cached (15s) estate snapshot. Only a fully-successful read is cached.

    A raise inside any one collector must never turn into a 500 for the
    others, so each is called on its own — none of the three can take the
    other two down. Caching is all-or-nothing across the three sources rather
    than per-source: a transient pool or runner outage must recover on the
    very next request, not sit cached for 15s next to two sources that are
    still fine.
    """
    global _cache
    started = time.monotonic()
    with _cache_lock:
        cached = _cache
        if cached is not None and not refresh and started - cached[0] < _CACHE_TTL_S:
            return cached[1]

    result = {
        "generated_at": utc_now_iso(),
        "pool": _safe_read(read_pool),
        "runners": _safe_read(read_runners),
        "local": _safe_read(read_local),
    }
    if _all_available(result):
        with _cache_lock:
            # Recheck: a slow read that STARTED before a fresher entry was
            # already stored must not overwrite it with stale evidence.
            if _cache is None or _cache[0] <= started:
                _cache = (time.monotonic(), result)
    return result


@router.get("/estate")
def get_estate(_: Annotated[None, Depends(_authorized)] = None) -> dict[str, Any]:
    """Live compute visibility: pool queue/machines, GH runners, this box."""
    del _
    return snapshot()


def _clear_cache() -> None:
    """Test support (mirrors testobs.readers._clear_cache) — not production API."""
    global _cache
    with _cache_lock:
        _cache = None


__all__ = ["router"]
