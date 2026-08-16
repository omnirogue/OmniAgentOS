"""Hard, synchronous, and pre-paid budget allocation ledger for the codebase-improvement lane.

Unlike the advisory, task-dependent posture of `omniagentos.budget.policy` which aims to keep
work moving without hard blockers for swarm runs, this module enforces strict, hard caps for the
automated codebase-improvement ('improve') lane.

In a concurrent multi-worker environment, audit checks executed with delays are insufficient
to prevent overshooting budget ceilings because multiple in-flight calls can proceed simultaneously
before the audit registers them. To solve this, this module mandates synchronous and pre-paid
budget admission: a reservation is made and counted against the caps *before* any paid provider
call is initiated.

Backstop
--------
This in-process ledger is the software's first line of defense. However, because a crashed host,
abrupt termination, or an out-of-band provider call might bypass this registry, each external
provider account must also be configured with a hard daily/monthly spend limit directly inside the
provider consoles:
- Anthropic Console -> Usage limits
- OpenAI Platform -> Limits -> monthly/daily budget
- Google AI/xAI Console equivalent spend caps
These out-of-band caps should be set at or slightly above the $200.0/day ceiling to ensure the
provider itself refuses requests before runaway billing can occur, and are deliberately NOT managed
or called from this codebase.
"""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from omniagentos.contracts import new_id

# Constants
DAILY_CEILING_USD = 200.0
POOL_CAPS: dict[str, float] = {
    "workers": 90.0,
    "cheap_judges": 10.0,
    "premium_panel": 25.0,
    "post_merge_verify": 10.0,
    "regression_analysis": 15.0,
    "reserve": 50.0,
}
# Assert that POOL_CAPS sums exactly to DAILY_CEILING_USD at import time
assert abs(sum(POOL_CAPS.values()) - DAILY_CEILING_USD) < 1e-9, (
    "POOL_CAPS must sum to DAILY_CEILING_USD"
)

NOTIFY_AT_FRACTION = 0.80
DEFAULT_RESERVATION_TTL_SECONDS = 900.0

_SECONDS_PER_DAY = 86400.0


def _utc_day_start(now: float) -> float:
    """Epoch seconds of the most recent UTC midnight at ``now``.

    Epoch time is UTC-based and excludes leap seconds, so UTC midnights fall
    on exact multiples of 86400 seconds.
    """
    return math.floor(now / _SECONDS_PER_DAY) * _SECONDS_PER_DAY


def _day_flag_active(value: object, day_start: float) -> bool:
    """True when a settings flag stamped with a UTC day-start matches ``day_start``.

    Flags store the day-start epoch they were set on; a flag stamped on another
    day (or a legacy '0'/'1' value) reads inactive, re-arming it for the
    current UTC day.
    """
    try:
        return float(str(value)) == day_start
    except (TypeError, ValueError):
        return False


# Exceptions
class BudgetError(RuntimeError):
    """Base exception for budget errors."""


class BudgetRefused(BudgetError):
    """Exception raised when a budget admission is refused."""

    def __init__(
        self,
        pool: str,
        requested_usd: float,
        settled_usd: float,
        outstanding_usd: float,
        cap_usd: float,
        reason: str,
    ) -> None:
        self.pool = pool
        self.requested_usd = requested_usd
        self.settled_usd = settled_usd
        self.outstanding_usd = outstanding_usd
        self.cap_usd = cap_usd
        self.reason = reason
        msg = (
            f"Budget admission refused for pool {pool!r}: {reason!r}. "
            f"Requested: {requested_usd:.4f} USD, "
            f"Settled: {settled_usd:.4f} USD, "
            f"Outstanding: {outstanding_usd:.4f} USD, "
            f"Cap: {cap_usd:.4f} USD."
        )
        super().__init__(msg)


class UnknownCostRefused(BudgetRefused):
    """Exception raised when an admission is refused because cost information is missing or invalid."""


class UnknownPool(BudgetError):
    """Exception raised when a specified pool is not recognized."""


# Dataclasses
@dataclass(frozen=True, slots=True)
class Reservation:
    id: str
    pool: str
    idempotency_key: str
    max_usd: float  # the ceiling this reservation holds
    state: str  # "open" | "settled" | "released" | "expired"
    created_at: float  # epoch seconds from the injected clock
    expires_at: float
    attempts: int  # how many provider attempts charged against it
    actual_usd: float | None


@dataclass(frozen=True, slots=True)
class PoolState:
    pool: str
    cap_usd: float
    settled_usd: float
    outstanding_usd: float
    committed_usd: float  # settled + outstanding
    available_usd: float  # max(0.0, cap - committed)
    fraction_used: float


# Helper database functions
def _connect(db_path: str) -> sqlite3.Connection:
    """Establish a connection to the SQLite database with house guidelines.

    Ensures WAL mode is active, foreign keys are enabled, busy_timeout is set,
    and row_factory uses Row.
    """
    if db_path != ":memory:":
        expanded = Path(db_path).expanduser()
        expanded.parent.mkdir(parents=True, exist_ok=True)
        db_path = str(expanded)
    connection = sqlite3.connect(
        db_path, isolation_level=None, timeout=5.0, check_same_thread=False
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection
    except BaseException:
        connection.close()
        raise


def _init_db(conn: sqlite3.Connection) -> None:
    """Initialize DB schema if it does not exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reservations (
            id TEXT PRIMARY KEY,
            pool TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            max_usd REAL NOT NULL,
            state TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            actual_usd REAL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_reservations_idempotency ON reservations(idempotency_key)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_reservations_state ON reservations(state)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS responses (
            idempotency_key TEXT PRIMARY KEY,
            response TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)


class BudgetLedger:
    """The hard budget admission ledger.

    Performs pre-paid synchronous reservation and cap tracking across pools.

    Caps are UTC-day scoped: only reservations whose created_at falls on the
    current UTC day (>= the current UTC midnight, derived from the injected
    clock) count toward DAILY_CEILING_USD and the pool caps. The pause flag is
    likewise day-scoped — see the `paused` property.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        pool_caps: Mapping[str, float] | None = None,
        ceiling_usd: float = DAILY_CEILING_USD,
        ttl_seconds: float = DEFAULT_RESERVATION_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        notifier: Callable[[str, str, float], None] | None = None,
    ) -> None:
        """Initialize the hard budget ledger.

        Cross-references omniagentos/budget/policy.py (which has advisory mode) to emphasize
        that this module represents a HARD blocking posture on purpose.
        """
        self._lock = RLock()
        self.pool_caps = dict(pool_caps) if pool_caps is not None else dict(POOL_CAPS)
        self.ceiling_usd = ceiling_usd
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self.notifier = notifier

        self._conn = _connect(str(db_path))
        with self._lock:
            _init_db(self._conn)

    def _row_to_reservation(self, row: sqlite3.Row) -> Reservation:
        """Map a sqlite Row to a Reservation dataclass."""
        return Reservation(
            id=row["id"],
            pool=row["pool"],
            idempotency_key=row["idempotency_key"],
            max_usd=row["max_usd"],
            state=row["state"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            attempts=row["attempts"],
            actual_usd=row["actual_usd"],
        )

    def _evaluate_pause_and_notify(self, pool: str, now: float) -> tuple[bool, bool, float]:
        """Evaluate the current-UTC-day committed spend against notification and pause thresholds.

        Must be called inside an active write transaction (e.g. before COMMIT).
        Only reservations created on the UTC day containing ``now`` count.
        The pause and notification flags are stamped with that day's UTC
        midnight so they expire (re-arm) at the next UTC midnight.
        Returns a tuple of (should_notify_80, should_notify_pause, fraction).
        """
        day_start = _utc_day_start(now)
        row = self._conn.execute(
            """
            SELECT
                SUM(CASE WHEN state = 'settled' THEN actual_usd ELSE 0.0 END) as settled,
                SUM(CASE WHEN state = 'open' THEN max_usd ELSE 0.0 END) as outstanding
            FROM reservations
            WHERE created_at >= ?
            """,
            (day_start,),
        ).fetchone()

        global_settled = row["settled"] if (row and row["settled"] is not None) else 0.0
        global_outstanding = row["outstanding"] if (row and row["outstanding"] is not None) else 0.0
        global_committed = global_settled + global_outstanding

        fraction = global_committed / self.ceiling_usd if self.ceiling_usd > 0.0 else 0.0

        should_notify_80 = False
        if fraction >= NOTIFY_AT_FRACTION:
            s_row = self._conn.execute(
                "SELECT value FROM settings WHERE key = 'notified_80'"
            ).fetchone()
            if not s_row or not _day_flag_active(s_row["value"], day_start):
                self._conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES ('notified_80', ?)",
                    (str(day_start),),
                )
                should_notify_80 = True

        should_notify_pause = False
        if global_committed >= self.ceiling_usd:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('paused', ?)",
                (str(day_start),),
            )
            pn_row = self._conn.execute(
                "SELECT value FROM settings WHERE key = 'notified_pause'"
            ).fetchone()
            if not pn_row or not _day_flag_active(pn_row["value"], day_start):
                self._conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES ('notified_pause', ?)",
                    (str(day_start),),
                )
                should_notify_pause = True

        return should_notify_80, should_notify_pause, fraction

    def get_reservation(self, reservation_id: str) -> Reservation:
        """Retrieve a Reservation by its ID.

        Raises BudgetError if not found.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
            ).fetchone()
            if not row:
                raise BudgetError(f"Reservation {reservation_id} not found")
            return self._row_to_reservation(row)

    def reserve(
        self,
        *,
        pool: str,
        estimated_max_usd: float | None,
        idempotency_key: str,
        ttl_seconds: float | None = None,
    ) -> Reservation:
        """The admission gate.

        Saves and holds budget before paid calls. Must run inside a single BEGIN IMMEDIATE tx.
        """
        if pool not in self.pool_caps:
            raise UnknownPool(f"Unknown pool: {pool}")

        now = self.clock()
        day_start = _utc_day_start(now)

        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")

                # 1. Reclaim expired reservations (state open and expires_at <= now -> expired)
                self._conn.execute(
                    "UPDATE reservations SET state = 'expired' WHERE state = 'open' AND expires_at <= ?",
                    (now,),
                )

                # 2. Check for existing OPEN reservation for idempotency_key
                row = self._conn.execute(
                    "SELECT * FROM reservations WHERE idempotency_key = ? AND state = 'open' LIMIT 1",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    self._conn.execute("COMMIT")
                    return self._row_to_reservation(row)

                # Or check for existing SETTLED reservation for idempotency_key
                row = self._conn.execute(
                    "SELECT * FROM reservations WHERE idempotency_key = ? AND state = 'settled' LIMIT 1",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    self._conn.execute("COMMIT")
                    return self._row_to_reservation(row)

                # Compute current-UTC-day pool metrics to populate potential refusal fields
                pool_data: dict[str, tuple[float, float]] = {}
                stats_rows = self._conn.execute(
                    """
                    SELECT pool,
                           SUM(CASE WHEN state = 'settled' THEN actual_usd ELSE 0.0 END) as settled,
                           SUM(CASE WHEN state = 'open' THEN max_usd ELSE 0.0 END) as outstanding
                    FROM reservations
                    WHERE created_at >= ?
                    GROUP BY pool
                    """,
                    (day_start,),
                ).fetchall()
                for r in stats_rows:
                    pool_data[r["pool"]] = (r["settled"], r["outstanding"])

                pool_settled, pool_outstanding = pool_data.get(pool, (0.0, 0.0))
                pool_cap = self.pool_caps[pool]

                global_settled = sum(settled for settled, _ in pool_data.values())
                global_outstanding = sum(outstanding for _, outstanding in pool_data.values())
                global_committed = global_settled + global_outstanding

                # 3. Check if estimated_max_usd is valid
                is_valid_cost = True
                if estimated_max_usd is None:
                    is_valid_cost = False
                else:
                    try:
                        val = float(estimated_max_usd)
                        if math.isnan(val) or val <= 0.0 or not math.isfinite(val):
                            is_valid_cost = False
                    except (ValueError, TypeError):
                        is_valid_cost = False

                if not is_valid_cost:
                    self._conn.execute("COMMIT")
                    raise UnknownCostRefused(
                        pool=pool,
                        requested_usd=estimated_max_usd if estimated_max_usd is not None else 0.0,
                        settled_usd=pool_settled,
                        outstanding_usd=pool_outstanding,
                        cap_usd=pool_cap,
                        reason="unknown_cost",
                    )

                assert estimated_max_usd is not None  # for mypy type checker to be happy

                # 4. Check if ledger is paused (only a pause stamped on the
                # current UTC day applies; yesterday's pause has expired)
                p_row = self._conn.execute(
                    "SELECT value FROM settings WHERE key = 'paused'"
                ).fetchone()
                if p_row and _day_flag_active(p_row["value"], day_start):
                    self._conn.execute("COMMIT")
                    raise BudgetRefused(
                        pool=pool,
                        requested_usd=estimated_max_usd,
                        settled_usd=global_settled,
                        outstanding_usd=global_outstanding,
                        cap_usd=self.ceiling_usd,
                        reason="paused",
                    )

                # 5. Check caps
                # Pool cap check
                if pool_settled + pool_outstanding + estimated_max_usd > pool_cap:
                    self._conn.execute("COMMIT")
                    raise BudgetRefused(
                        pool=pool,
                        requested_usd=estimated_max_usd,
                        settled_usd=pool_settled,
                        outstanding_usd=pool_outstanding,
                        cap_usd=pool_cap,
                        reason="pool_cap_exceeded",
                    )

                # Global ceiling check
                if global_committed + estimated_max_usd > self.ceiling_usd:
                    self._conn.execute("COMMIT")
                    raise BudgetRefused(
                        pool=pool,
                        requested_usd=estimated_max_usd,
                        settled_usd=global_settled,
                        outstanding_usd=global_outstanding,
                        cap_usd=self.ceiling_usd,
                        reason="daily_ceiling_exceeded",
                    )

                # 6. Insert new open reservation
                res_id = new_id("res")
                expires_at = now + (ttl_seconds if ttl_seconds is not None else self.ttl_seconds)
                self._conn.execute(
                    """
                    INSERT INTO reservations (id, pool, idempotency_key, max_usd, state, created_at, expires_at, attempts, actual_usd)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        res_id,
                        pool,
                        idempotency_key,
                        estimated_max_usd,
                        "open",
                        now,
                        expires_at,
                        0,
                        None,
                    ),
                )

                # Evaluate pause and notification triggers
                should_notify_80, should_notify_pause, fraction_after = (
                    self._evaluate_pause_and_notify(pool, now)
                )

                self._conn.execute("COMMIT")

                # Handle notifications outside the transaction context
                if self.notifier:
                    if should_notify_80:
                        try:
                            self.notifier(pool, "warning", fraction_after)
                        except Exception:
                            pass
                    if should_notify_pause:
                        try:
                            self.notifier(pool, "pause", fraction_after)
                        except Exception:
                            pass

                return Reservation(
                    id=res_id,
                    pool=pool,
                    idempotency_key=idempotency_key,
                    max_usd=estimated_max_usd,
                    state="open",
                    created_at=now,
                    expires_at=expires_at,
                    attempts=0,
                    actual_usd=None,
                )

            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

    def settle(
        self,
        reservation_id: str,
        *,
        actual_usd: float | None,
        usage_available: bool = True,
    ) -> Reservation:
        """Convert a held reservation into settled spend.

        FAIL CLOSED: if actual_usd is None, negative, not finite, or
        usage_available is False, charge the FULL reserved max_usd, never 0.0.
        Clamp a reported actual ABOVE max_usd up to the reported value but record
        an overshoot; an actual BELOW max_usd (but non-negative) releases the
        difference. Settling an already-settled reservation is idempotent.
        """
        now = self.clock()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
                ).fetchone()
                if not row:
                    raise BudgetError(f"Reservation {reservation_id} not found")

                res = self._row_to_reservation(row)
                if res.state == "settled":
                    self._conn.execute("COMMIT")
                    return res

                if res.state != "open":
                    raise BudgetError(
                        f"Reservation {reservation_id} is in state {res.state!r}, cannot settle"
                    )

                is_valid_actual = True
                if actual_usd is None or not usage_available:
                    is_valid_actual = False
                else:
                    try:
                        val = float(actual_usd)
                        # Negative is invalid telemetry (same class as NaN/inf/None):
                        # never record it as free 0.0 spend against the cap.
                        if math.isnan(val) or not math.isfinite(val) or val < 0.0:
                            is_valid_actual = False
                    except (ValueError, TypeError):
                        is_valid_actual = False

                if not is_valid_actual:
                    resolved_usd = res.max_usd
                else:
                    assert actual_usd is not None
                    resolved_usd = max(0.0, float(actual_usd))

                self._conn.execute(
                    "UPDATE reservations SET state = 'settled', actual_usd = ? WHERE id = ?",
                    (resolved_usd, reservation_id),
                )

                updated_row = self._conn.execute(
                    "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
                ).fetchone()
                updated_res = self._row_to_reservation(updated_row)

                # Evaluate pause and notification triggers
                should_notify_80, should_notify_pause, fraction_after = (
                    self._evaluate_pause_and_notify(updated_res.pool, now)
                )

                self._conn.execute("COMMIT")

                # Handle notifications outside the transaction context
                if self.notifier:
                    if should_notify_80:
                        try:
                            self.notifier(updated_res.pool, "warning", fraction_after)
                        except Exception:
                            pass
                    if should_notify_pause:
                        try:
                            self.notifier(updated_res.pool, "pause", fraction_after)
                        except Exception:
                            pass

                return updated_res
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

    def release(self, reservation_id: str) -> Reservation:
        """Release a held reservation (the call never happened, e.g. cancelled/refused).

        Frees the hold, charges nothing. Idempotent.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
                ).fetchone()
                if not row:
                    raise BudgetError(f"Reservation {reservation_id} not found")

                res = self._row_to_reservation(row)
                if res.state == "released":
                    self._conn.execute("COMMIT")
                    return res

                if res.state != "open":
                    raise BudgetError(
                        f"Reservation {reservation_id} is in state {res.state!r}, cannot release"
                    )

                self._conn.execute(
                    "UPDATE reservations SET state = 'released', actual_usd = 0.0 WHERE id = ?",
                    (reservation_id,),
                )

                updated_row = self._conn.execute(
                    "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
                ).fetchone()
                updated_res = self._row_to_reservation(updated_row)

                self._conn.execute("COMMIT")
                return updated_res
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

    def record_attempt(self, reservation_id: str) -> Reservation:
        """Increment attempts for a reservation.

        Raises BudgetError if the reservation is not open.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
                ).fetchone()
                if not row:
                    raise BudgetError(f"Reservation {reservation_id} not found")

                res = self._row_to_reservation(row)
                if res.state != "open":
                    raise BudgetError(
                        f"Reservation {reservation_id} is not open (state: {res.state!r})"
                    )

                self._conn.execute(
                    "UPDATE reservations SET attempts = attempts + 1 WHERE id = ?",
                    (reservation_id,),
                )

                updated_row = self._conn.execute(
                    "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
                ).fetchone()
                updated_res = self._row_to_reservation(updated_row)

                self._conn.execute("COMMIT")
                return updated_res
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

    def remember_response(self, idempotency_key: str, response: str) -> None:
        """Store response dedup for a given idempotency key."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT OR REPLACE INTO responses (idempotency_key, response) VALUES (?, ?)",
                    (idempotency_key, response),
                )
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

    def replay(self, idempotency_key: str) -> str | None:
        """Retrieve the stored response for a given idempotency key, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT response FROM responses WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            return str(row["response"]) if row else None

    def reclaim_expired(self, *, now: float | None = None) -> list[str]:
        """Sweep: open reservations past expires_at become 'expired'.

        Returns the ids reclaimed. Idempotent, safe to call concurrently.
        """
        if now is None:
            now = self.clock()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                rows = self._conn.execute(
                    "SELECT id FROM reservations WHERE state = 'open' AND expires_at <= ?",
                    (now,),
                ).fetchall()
                reclaimed_ids = [str(r["id"]) for r in rows]
                if reclaimed_ids:
                    self._conn.execute(
                        "UPDATE reservations SET state = 'expired' WHERE state = 'open' AND expires_at <= ?",
                        (now,),
                    )
                self._conn.execute("COMMIT")
                return reclaimed_ids
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

    def pool_state(self, pool: str) -> PoolState:
        """Retrieve the current-UTC-day state of a specific pool."""
        if pool not in self.pool_caps:
            raise UnknownPool(f"Unknown pool: {pool}")

        day_start = _utc_day_start(self.clock())
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    SUM(CASE WHEN state = 'settled' THEN actual_usd ELSE 0.0 END) as settled,
                    SUM(CASE WHEN state = 'open' THEN max_usd ELSE 0.0 END) as outstanding
                FROM reservations
                WHERE pool = ? AND created_at >= ?
                """,
                (pool, day_start),
            ).fetchone()

            settled_usd = row["settled"] if (row and row["settled"] is not None) else 0.0
            outstanding_usd = (
                row["outstanding"] if (row and row["outstanding"] is not None) else 0.0
            )
            cap_usd = self.pool_caps[pool]
            committed_usd = settled_usd + outstanding_usd
            available_usd = max(0.0, cap_usd - committed_usd)
            fraction_used = committed_usd / cap_usd if cap_usd > 0.0 else 0.0

            return PoolState(
                pool=pool,
                cap_usd=cap_usd,
                settled_usd=settled_usd,
                outstanding_usd=outstanding_usd,
                committed_usd=committed_usd,
                available_usd=available_usd,
                fraction_used=fraction_used,
            )

    def snapshot(self) -> dict[str, Any]:
        """Retrieve a snapshot of the current-UTC-day ledger state."""
        day_start = _utc_day_start(self.clock())
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    SUM(CASE WHEN state = 'settled' THEN actual_usd ELSE 0.0 END) as settled,
                    SUM(CASE WHEN state = 'open' THEN max_usd ELSE 0.0 END) as outstanding
                FROM reservations
                WHERE created_at >= ?
                """,
                (day_start,),
            ).fetchone()

            global_settled = row["settled"] if (row and row["settled"] is not None) else 0.0
            global_outstanding = (
                row["outstanding"] if (row and row["outstanding"] is not None) else 0.0
            )
            global_committed = global_settled + global_outstanding
            global_available = max(0.0, self.ceiling_usd - global_committed)
            global_fraction = global_committed / self.ceiling_usd if self.ceiling_usd > 0.0 else 0.0

            pool_states = {pool: self.pool_state(pool) for pool in self.pool_caps}

            return {
                "pools": pool_states,
                "global_committed_usd": global_committed,
                "global_available_usd": global_available,
                "global_fraction_used": global_fraction,
                "paused": self.paused,
            }

    @property
    def paused(self) -> bool:
        """Return True if the ledger is paused for the CURRENT UTC day.

        Pause is day-scoped, not lifetime: hitting the daily ceiling stamps the
        pause flag with the UTC day it tripped on, and only that day reads as
        paused. After the next UTC midnight the flag reads False and reserve()
        admits again with no explicit resume() — nothing is mutated on read or
        on a new-day reserve; the stale stamp is simply re-written the next
        time the ceiling trips. resume() still clears a same-day pause early.
        """
        day_start = _utc_day_start(self.clock())
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key = 'paused'").fetchone()
            return row is not None and _day_flag_active(row["value"], day_start)

    def resume(self) -> None:
        """Clear the pause state and pause notification state."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES ('paused', '0')"
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES ('notified_pause', '0')"
                )
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

    def close(self) -> None:
        """Close the SQLite database connection."""
        with self._lock:
            self._conn.close()


@contextmanager
def paid_call(
    ledger: BudgetLedger,
    *,
    pool: str,
    estimated_max_usd: float | None,
    idempotency_key: str,
) -> Iterator[Reservation]:
    """Context manager for managing the lifecycle of a paid provider call.

    This ensures that budget is synchronously reserved before the call and settled afterwards.

    Lifecycle:
    1. Synchronously reserves the estimated maximum budget.
    2. Yields the created Reservation.
    3. If the code block exits cleanly, settles the reservation. If no explicit settle happened
       (i.e., the reservation remains "open"), we fail closed and settle at the reserved max_usd.
    4. If an exception occurs, we check the latest state of the reservation:
       - If no provider attempts were recorded (attempts == 0), the paid call never took place
         (e.g., failed in the client or pre-flight check), so we release the hold completely.
       - If provider attempts were recorded (attempts > 0), money was likely spent, so we fail
         closed and settle at the reserved max_usd to ensure our budget tracks the potential spend.
    """
    reservation = ledger.reserve(
        pool=pool,
        estimated_max_usd=estimated_max_usd,
        idempotency_key=idempotency_key,
    )
    try:
        yield reservation
    except BaseException:
        current = ledger.get_reservation(reservation.id)
        if current.attempts == 0:
            ledger.release(reservation.id)
        else:
            ledger.settle(reservation.id, actual_usd=reservation.max_usd)
        raise
    else:
        current = ledger.get_reservation(reservation.id)
        if current.state == "open":
            ledger.settle(reservation.id, actual_usd=current.max_usd)


__all__ = [
    "DAILY_CEILING_USD",
    "POOL_CAPS",
    "NOTIFY_AT_FRACTION",
    "DEFAULT_RESERVATION_TTL_SECONDS",
    "BudgetError",
    "BudgetRefused",
    "UnknownCostRefused",
    "UnknownPool",
    "Reservation",
    "PoolState",
    "BudgetLedger",
    "paid_call",
]
