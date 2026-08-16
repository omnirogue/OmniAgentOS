"""Bounded SQLite BUSY/LOCKED retry primitive (M-33).

Shared contract for every store that writes through SQLite. L14 owns this module
under ``omniagentos/db/**``; L09 (CSI) and L10 (swarm) adopt it later without
editing their exclusive production paths from this lane.

Transaction-safe semantics
--------------------------
``run_with_busy_retry`` re-invokes the whole callable after a contention error.
Callers must structure the callable as one complete unit of work:

* open transaction (``BEGIN IMMEDIATE`` preferred for writers)
* execute statements
* ``COMMIT`` on success, ``ROLLBACK`` on any error before re-raise

Never retry mid-transaction partial work, and never nest a busy-retry body
inside an already-open outer transaction: an inner rollback would undo the
outer unit (see synthesis residual M-40).

``execute_write_transaction`` implements that shape for a single connection
(and optional process-level writer lock acquired *inside* each attempt so
backoff sleeps release the lock).

Observability
-------------
Process-wide counters in :data:`BUSY_RETRY_METRICS` track attempts, retries,
successes, exhaustion, and wait time. Tests may call :func:`reset_busy_retry_metrics`.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

_LOG = logging.getLogger(__name__)

# Primary codes (sqlite3.Error.sqlite_errorcode & 0xFF) when present.
_SQLITE_BUSY = int(getattr(sqlite3, "SQLITE_BUSY", 5))
_SQLITE_LOCKED = int(getattr(sqlite3, "SQLITE_LOCKED", 6))


@dataclass(frozen=True, slots=True)
class BusyRetryPolicy:
    """Bounded exponential backoff + jitter for SQLITE_BUSY / LOCKED."""

    max_attempts: int = 10
    base_delay_ms: float = 10.0
    max_delay_ms: float = 500.0
    jitter_ms: float = 100.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_ms < 0 or self.max_delay_ms < 0 or self.jitter_ms < 0:
            raise ValueError("delays must be non-negative")


DEFAULT_BUSY_RETRY_POLICY = BusyRetryPolicy()


@dataclass(slots=True)
class BusyRetryMetrics:
    """Contention observability counters (process-wide or local)."""

    attempts: int = 0
    retries: int = 0
    successes: int = 0
    exhausted: int = 0
    total_wait_ms: float = 0.0
    last_error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def snapshot(self) -> dict[str, float | int | str | None]:
        with self._lock:
            return {
                "attempts": self.attempts,
                "retries": self.retries,
                "successes": self.successes,
                "exhausted": self.exhausted,
                "total_wait_ms": round(self.total_wait_ms, 3),
                "last_error": self.last_error,
            }

    def reset(self) -> None:
        with self._lock:
            self.attempts = 0
            self.retries = 0
            self.successes = 0
            self.exhausted = 0
            self.total_wait_ms = 0.0
            self.last_error = None

    def record_attempt(self) -> None:
        with self._lock:
            self.attempts += 1

    def record_retry(self, wait_ms: float, error: BaseException) -> None:
        with self._lock:
            self.retries += 1
            self.total_wait_ms += wait_ms
            self.last_error = str(error)

    def record_success(self) -> None:
        with self._lock:
            self.successes += 1

    def record_exhausted(self, error: BaseException) -> None:
        with self._lock:
            self.exhausted += 1
            self.last_error = str(error)


#: Process-wide contention counters. Shared by all adopters unless they pass a
#: private :class:`BusyRetryMetrics` into :func:`run_with_busy_retry`.
BUSY_RETRY_METRICS = BusyRetryMetrics()


class BusyRetryExhausted(sqlite3.OperationalError):
    """Raised when bounded busy/locked retries are exhausted.

    Subclasses ``sqlite3.OperationalError`` so existing ``except OperationalError``
    handlers keep working, while callers that care can detect exhaustion.
    """

    def __init__(self, message: str, *, attempts: int, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts
        if cause is not None:
            self.__cause__ = cause


def is_busy_or_locked(error: BaseException) -> bool:
    """Return True when ``error`` is SQLite BUSY or LOCKED contention."""
    # Exhaustion is a terminal wrapper outcome, not a fresh SQLite contention
    # signal. Treating its message as BUSY would multiply nested retry budgets.
    if isinstance(error, BusyRetryExhausted):
        return False
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int) and (code & 0xFF) in {_SQLITE_BUSY, _SQLITE_LOCKED}:
        return True
    message = str(error).lower()
    return (
        "database is locked" in message
        or "database table is locked" in message
        or "sqlite_busy" in message
        or "sqlite_locked" in message
    )


def compute_backoff_ms(attempt: int, policy: BusyRetryPolicy) -> float:
    """Exponential backoff for zero-based ``attempt``, capped, plus jitter."""
    exp = policy.base_delay_ms * (2**attempt)
    capped = min(exp, policy.max_delay_ms)
    if policy.jitter_ms <= 0:
        return capped
    return capped + random.uniform(0.0, policy.jitter_ms)


def run_with_busy_retry[R](
    fn: Callable[[], R],
    *,
    policy: BusyRetryPolicy | None = None,
    metrics: BusyRetryMetrics | None = None,
    op: str = "sqlite_write",
) -> R:
    """Run ``fn`` with bounded busy/locked retries and observability.

    ``fn`` must be a full transaction unit (see module docstring). Non-contention
    errors propagate immediately without retry.
    """
    active_policy = policy or DEFAULT_BUSY_RETRY_POLICY
    counters = metrics if metrics is not None else BUSY_RETRY_METRICS
    last_error: BaseException | None = None

    for attempt in range(active_policy.max_attempts):
        counters.record_attempt()
        try:
            result = fn()
        except BaseException as error:
            if not is_busy_or_locked(error):
                raise
            last_error = error
            if attempt >= active_policy.max_attempts - 1:
                counters.record_exhausted(error)
                _LOG.error(
                    "sqlite_busy_retry_exhausted op=%s attempts=%d error=%s",
                    op,
                    active_policy.max_attempts,
                    error,
                )
                raise BusyRetryExhausted(
                    f"SQLITE_BUSY/LOCKED retries exhausted for {op!r} "
                    f"after {active_policy.max_attempts} attempts: {error}",
                    attempts=active_policy.max_attempts,
                    cause=error,
                ) from error

            wait_ms = compute_backoff_ms(attempt, active_policy)
            counters.record_retry(wait_ms, error)
            _LOG.warning(
                "sqlite_busy_retry op=%s attempt=%d/%d wait_ms=%.1f error=%s",
                op,
                attempt + 1,
                active_policy.max_attempts,
                wait_ms,
                error,
            )
            time.sleep(wait_ms / 1000.0)
            continue

        counters.record_success()
        return result

    # Unreachable: loop either returns or raises. Keep mypy/runtime honest.
    raise BusyRetryExhausted(
        f"SQLITE_BUSY/LOCKED retries exhausted for {op!r}",
        attempts=active_policy.max_attempts,
        cause=last_error,
    )


def execute_write_transaction[R](
    connection: sqlite3.Connection,
    body: Callable[[sqlite3.Connection], R],
    *,
    lock: threading.RLock | None = None,
    policy: BusyRetryPolicy | None = None,
    metrics: BusyRetryMetrics | None = None,
    op: str = "sqlite_write_txn",
    immediate: bool = True,
) -> R:
    """Run ``body`` inside one BEGIN/COMMIT with busy retry of the whole unit.

    When ``lock`` is provided it is acquired *inside* each attempt so backoff
    sleeps never hold the process writer lock. Residual transactions are
    rolled back before ``BEGIN`` so a prior failed attempt cannot leave the
    connection half-open.
    """

    begin_sql = "BEGIN IMMEDIATE" if immediate else "BEGIN"

    def attempt() -> R:
        if lock is not None:
            with lock:
                return _run_one_transaction(connection, body, begin_sql)
        return _run_one_transaction(connection, body, begin_sql)

    return run_with_busy_retry(attempt, policy=policy, metrics=metrics, op=op)


def _run_one_transaction[R](
    connection: sqlite3.Connection,
    body: Callable[[sqlite3.Connection], R],
    begin_sql: str,
) -> R:
    try:
        connection.rollback()
    except sqlite3.OperationalError:
        pass

    connection.execute(begin_sql)
    try:
        result = body(connection)
        connection.commit()
        return result
    except BaseException:
        try:
            connection.rollback()
        except sqlite3.OperationalError:
            pass
        raise


def reset_busy_retry_metrics() -> None:
    """Clear process-wide counters (test helper)."""
    BUSY_RETRY_METRICS.reset()


def busy_retry_metrics_snapshot() -> dict[str, float | int | str | None]:
    """Return a copy of process-wide contention counters."""
    return BUSY_RETRY_METRICS.snapshot()


# Narrow handoff surface for L09/L10 (and any other store) adoption.
__all__ = [
    "BUSY_RETRY_METRICS",
    "BusyRetryExhausted",
    "BusyRetryMetrics",
    "BusyRetryPolicy",
    "DEFAULT_BUSY_RETRY_POLICY",
    "busy_retry_metrics_snapshot",
    "compute_backoff_ms",
    "execute_write_transaction",
    "is_busy_or_locked",
    "reset_busy_retry_metrics",
    "run_with_busy_retry",
]
