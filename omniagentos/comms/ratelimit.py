"""A small in-process token bucket for coarse per-source inbound rate limiting.

Deliberately not distributed/persistent: the Steward control plane is a single
localhost process (see contracts.py API_HOST), so per-process state is sufficient
and keeps the webhook request path free of any extra network/DB round trip.
"""

from __future__ import annotations

import threading
import time

__all__ = ["RateLimiter"]


class _TokenBucket:
    def __init__(self, capacity: float, refill_per_minute: float) -> None:
        self._capacity = max(capacity, 1.0)
        self._refill_per_second = max(refill_per_minute, 0.0) / 60.0
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = max(0.0, now - self._updated)
            self._updated = now
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_second)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


class RateLimiter:
    """Per-key token buckets, created lazily on first use of a key."""

    def __init__(self) -> None:
        self._buckets: dict[str, _TokenBucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, rate_per_minute: float) -> bool:
        """Consume one token for ``key``; ``False`` means the caller should be rate-limited."""
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _TokenBucket(rate_per_minute, rate_per_minute)
                self._buckets[key] = bucket
        return bucket.allow()

    def reset(self) -> None:
        """Clear all bucket state (test isolation only)."""
        with self._lock:
            self._buckets.clear()
