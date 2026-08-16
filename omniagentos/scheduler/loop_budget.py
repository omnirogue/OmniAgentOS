"""Hard, synchronous, pre-paid budget ledger for loop capability execution.

This ledger enforces strict spend caps on paid loop capabilities:
- Global daily ceiling (USD per UTC day)
- Per-loop-instance daily cap (USD per UTC day, per instance_id)
- A single runaway loop cannot consume the whole day's budget

Unlike ``omniagentos.improve.budget``, which pools multiple workers into shared
pools, each loop instance is its own admission unit. The per-instance cap
prevents one misconfigured or compromised loop from burning the daily budget,
and the global ceiling still binds when multiple instances are running.

In a concurrent multi-worker environment, audit checks executed with delays
are insufficient to prevent overshooting budgets because multiple in-flight
calls can proceed simultaneously before the audit registers them. To solve this,
this module mandates synchronous and pre-paid budget admission: a reservation
is made and counted against the caps *BEFORE* any paid provider call is
initiated. This is called from ``loop_effects.execute()`` before
``broker.call``.
"""

from __future__ import annotations

import contextlib
import logging
import math
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from omniagentos.contracts import new_id

logger = logging.getLogger(__name__)

# Constants
GLOBAL_DAILY_CEILING_USD = 200.0

#: Default per-instance daily cap. A loop instance cannot spend more than this
#: in a single UTC day, even if the global ceiling allows it. If a single cap
#: needs to be raised, it lives in code here, never in config or the database.
DEFAULT_INSTANCE_CAP_USD = 50.0

#: Per-instance caps. Keys are instance_id, values are the USD ceiling for
#: that instance on any single UTC day. An instance not listed gets
#: DEFAULT_INSTANCE_CAP_USD. These are code-defined, not database-mutable,
#: because a cap is an authorization decision: a loop operator must not be
#: able to raise its own ceiling by editing a row.
INSTANCE_CAPS: dict[str, float] = {
    # The seam's own end-to-end control loop, used for testing and validation.
    # It generates one image per tick, roughly 2-4 USD per image.
    "render_probe": 10.0,
}

NOTIFY_AT_FRACTION = 0.80
DEFAULT_RESERVATION_TTL_SECONDS = 900.0

#: Reservation states, as stored.
STATE_OPEN = "open"
STATE_SETTLED = "settled"
STATE_RELEASED = "released"

#: A reservation whose owner never came back. The process was killed between
#: the reserve and the settle, or ``settle()`` itself failed — so the call may
#: well have been made and paid for, and nobody is left to say. It is CHARGED
#: at its reserved maximum with ``cost_quality='unknown'``: an expiry is not
#: evidence that a call did not happen, and the one thing this ledger must
#: never do is hand back money for an effect that may have executed.
#:
#: WHY CHARGE RATHER THAN ALARM-AND-FREE. The alternative — mark it unknown,
#: page an operator, leave the budget available — is fail-OPEN in exactly the
#: window where it matters: a loop that crashes between reserve and settle
#: every tick would have its cap reset every TTL, so the real ceiling becomes
#: "cap per 15 minutes" instead of "cap per day", and it stays that way until
#: a human reads the alarm. Over-counting a call that never happened costs at
#: most ``max_usd`` of headroom for the rest of the UTC day and is visible and
#: correctable; under-counting a call that did happen costs real money at a
#: rate nothing bounds. Everything else in this seam fails closed; so does this.
#: The alarm is kept as well — charging silently would hide a crash loop.
STATE_EXPIRED_UNKNOWN = "expired_unknown"

#: States whose ``actual_usd`` counts as spend against the day's caps.
_CHARGED_STATES = (STATE_SETTLED, STATE_EXPIRED_UNKNOWN)

_SECONDS_PER_DAY = 86400.0

#: The states above, spelled for SQL. Derived from the tuple so a rename cannot
#: leave one query counting a state another query has stopped counting — the
#: divergence that would make the cap and the report disagree.
_CHARGED_STATES_SQL = ", ".join(f"'{state}'" for state in _CHARGED_STATES)

#: The three money columns, defined ONCE. ``reserve`` admits against them and
#: ``get_instance_state`` reports them, and those two must never be able to
#: drift apart.
_SPENT_SQL = f"CASE WHEN state IN ({_CHARGED_STATES_SQL}) THEN COALESCE(actual_usd, 0.0) ELSE 0.0 END"
_OUTSTANDING_SQL = f"CASE WHEN state = '{STATE_OPEN}' THEN max_usd ELSE 0.0 END"
_UNACCOUNTED_SQL = (
    f"CASE WHEN state = '{STATE_EXPIRED_UNKNOWN}' THEN COALESCE(actual_usd, 0.0) ELSE 0.0 END"
)


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
    """Exception raised when a budget admission is refused (adverse verdict).

    Machine-readable outcome: this is a REACHED authority (the budget ledger)
    refusing. It is an adverse fact and settles against the floor, not neutral.
    """

    def __init__(
        self,
        instance_id: str,
        requested_usd: float,
        settled_usd: float,
        outstanding_usd: float,
        cap_usd: float,
        ceiling_usd: float,
        reason: str,
    ) -> None:
        self.instance_id = instance_id
        self.requested_usd = requested_usd
        self.settled_usd = settled_usd
        self.outstanding_usd = outstanding_usd
        self.cap_usd = cap_usd
        self.ceiling_usd = ceiling_usd
        self.reason = reason
        msg = (
            f"Loop budget admission refused for {instance_id!r}: {reason!r}. "
            f"Requested: {requested_usd:.4f} USD, "
            f"Settled: {settled_usd:.4f} USD, "
            f"Outstanding: {outstanding_usd:.4f} USD, "
            f"Instance cap: {cap_usd:.4f} USD, "
            f"Global ceiling: {ceiling_usd:.4f} USD."
        )
        super().__init__(msg)


class UnknownCostRefused(BudgetRefused):
    """Exception raised when admission is refused because cost is missing or invalid."""


# Dataclasses
@dataclass(frozen=True, slots=True)
class Reservation:
    id: str
    instance_id: str
    capability_id: str
    max_usd: float  # the ceiling this reservation holds
    state: str  # "open" | "settled" | "released" | "expired_unknown"
    created_at: float  # epoch seconds from the injected clock
    expires_at: float
    attempts: int
    actual_usd: float | None
    cost_quality: str  # "exact" | "estimated" | "unknown"


@dataclass(frozen=True, slots=True)
class InstanceState:
    instance_id: str
    cap_usd: float
    settled_usd: float  # charged spend: real settles PLUS expired-unknown charges
    outstanding_usd: float
    committed_usd: float  # settled + outstanding
    available_usd: float  # max(0.0, cap - committed)
    fraction_used: float
    #: The part of ``settled_usd`` that was charged because a reservation
    #: expired without its owner settling it, i.e. money the books had to
    #: assume was spent. Nonzero means something died mid-effect; it is the
    #: operator-visible half of the expire-as-spent rule.
    unaccounted_usd: float = 0.0


# Helper database functions
def _connect(db_path: str) -> sqlite3.Connection:
    """Establish a connection with WAL mode and house guidelines."""
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
        CREATE TABLE IF NOT EXISTS loop_reservations (
            id TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            capability_id TEXT NOT NULL,
            max_usd REAL NOT NULL,
            state TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            actual_usd REAL,
            cost_quality TEXT NOT NULL DEFAULT 'unknown'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_loop_res_instance ON loop_reservations(instance_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_loop_res_capability ON loop_reservations(capability_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_loop_res_state ON loop_reservations(state)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS loop_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)


class LoopBudgetLedger:
    """Hard budget admission ledger for loop capability execution.

    Performs pre-paid synchronous reservation and cap tracking for each loop
    instance, plus a global ceiling.

    Caps are UTC-day scoped: only reservations whose created_at falls on the
    current UTC day count toward the per-instance cap and GLOBAL_DAILY_CEILING_USD.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        instance_caps: Mapping[str, float] | None = None,
        default_instance_cap_usd: float = DEFAULT_INSTANCE_CAP_USD,
        global_ceiling_usd: float = GLOBAL_DAILY_CEILING_USD,
        ttl_seconds: float = DEFAULT_RESERVATION_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
        notifier: Callable[[str, str, str, float], None] | None = None,
    ) -> None:
        """Initialize the hard budget ledger.

        Args:
            db_path: Path to the SQLite database.
            instance_caps: Mapping of instance_id -> cap_usd. Instances not
                listed get default_instance_cap_usd.
            default_instance_cap_usd: Default per-instance daily cap.
            global_ceiling_usd: Global daily ceiling across all instances.
            ttl_seconds: TTL for open reservations.
            clock: Injected clock (for testing).
            notifier: Optional callback (instance_id, level, reason, fraction).
        """
        self._lock = RLock()
        self.instance_caps = (
            dict(instance_caps) if instance_caps is not None else dict(INSTANCE_CAPS)
        )
        self.default_instance_cap_usd = default_instance_cap_usd
        self.global_ceiling_usd = global_ceiling_usd
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self.notifier = notifier

        self._conn = _connect(str(db_path))
        with self._lock:
            _init_db(self._conn)

    def _get_instance_cap(self, instance_id: str) -> float:
        """Get the cap for an instance (from map or default)."""
        return self.instance_caps.get(instance_id, self.default_instance_cap_usd)

    def _row_to_reservation(self, row: sqlite3.Row) -> Reservation:
        """Map a sqlite Row to a Reservation dataclass."""
        return Reservation(
            id=row["id"],
            instance_id=row["instance_id"],
            capability_id=row["capability_id"],
            max_usd=row["max_usd"],
            state=row["state"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            attempts=row["attempts"],
            actual_usd=row["actual_usd"],
            cost_quality=row["cost_quality"] or "unknown",
        )

    def get_reservation(self, reservation_id: str) -> Reservation:
        """Retrieve a Reservation by its ID. Raises BudgetError if not found."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM loop_reservations WHERE id = ?", (reservation_id,)
            ).fetchone()
            if not row:
                raise BudgetError(f"Reservation {reservation_id} not found")
            return self._row_to_reservation(row)

    def reserve(
        self,
        *,
        instance_id: str,
        capability_id: str,
        estimated_max_usd: float | None,
        idempotency_key: str | None = None,
        ttl_seconds: float | None = None,
    ) -> Reservation:
        """The admission gate: refuse or return a reservation BEFORE any call.

        Pre-paid admission ensures caps are enforced before paid calls happen.
        The reservation holds budget until settled (cost is real) or released
        (call never happened).

        Args:
            instance_id: The loop instance requesting the capability.
            capability_id: The capability being requested (e.g., 'replicate.generate').
            estimated_max_usd: Max USD the call might cost. None/NaN/negative/inf
                are invalid and raise UnknownCostRefused.
            idempotency_key: Optional key for deduping retries (not implemented
                yet, reserved for future use).
            ttl_seconds: TTL for this reservation (default inherited).

        Returns:
            A Reservation in state='open', holding the budget.

        Raises:
            BudgetRefused (or subclass): Admission refused. The reason is
                machine-readable. This is an ADVERSE outcome.
            UnknownCostRefused: Cost is invalid.
        """
        now = self.clock()
        day_start = _utc_day_start(now)

        # Sweep leaked reservations FIRST, in their own committed transaction,
        # so the day's metrics computed below already include what the sweep
        # charged. It is the same code path an operator gets from
        # ``reclaim_expired`` — one definition of "what an expiry means", not
        # two that can drift.
        self.reclaim_expired(now=now)

        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")

                # 2. Validate cost
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
                    instance_cap = self._get_instance_cap(instance_id)
                    raise UnknownCostRefused(
                        instance_id=instance_id,
                        requested_usd=estimated_max_usd if estimated_max_usd is not None else 0.0,
                        settled_usd=0.0,
                        outstanding_usd=0.0,
                        cap_usd=instance_cap,
                        ceiling_usd=self.global_ceiling_usd,
                        reason="unknown_cost",
                    )

                assert estimated_max_usd is not None

                # 3. Compute current-UTC-day metrics
                global_row = self._conn.execute(
                    f"""
                    SELECT
                        SUM({_SPENT_SQL}) as global_settled,
                        SUM({_OUTSTANDING_SQL}) as global_outstanding
                    FROM loop_reservations
                    WHERE created_at >= ?
                    """,  # noqa: S608 — the interpolations are module constants
                    (day_start,),
                ).fetchone()

                global_settled = (
                    global_row["global_settled"] if global_row and global_row["global_settled"] else 0.0
                )
                global_outstanding = (
                    global_row["global_outstanding"] if global_row and global_row["global_outstanding"] else 0.0
                )
                global_committed = global_settled + global_outstanding

                instance_row = self._conn.execute(
                    f"""
                    SELECT
                        SUM({_SPENT_SQL}) as inst_settled,
                        SUM({_OUTSTANDING_SQL}) as inst_outstanding
                    FROM loop_reservations
                    WHERE instance_id = ? AND created_at >= ?
                    """,  # noqa: S608 — the interpolations are module constants
                    (instance_id, day_start),
                ).fetchone()

                inst_settled = instance_row["inst_settled"] if instance_row and instance_row["inst_settled"] else 0.0
                inst_outstanding = (
                    instance_row["inst_outstanding"] if instance_row and instance_row["inst_outstanding"] else 0.0
                )
                inst_committed = inst_settled + inst_outstanding

                instance_cap = self._get_instance_cap(instance_id)

                # 4. Check caps
                if inst_committed + estimated_max_usd > instance_cap:
                    self._conn.execute("COMMIT")
                    raise BudgetRefused(
                        instance_id=instance_id,
                        requested_usd=estimated_max_usd,
                        settled_usd=inst_settled,
                        outstanding_usd=inst_outstanding,
                        cap_usd=instance_cap,
                        ceiling_usd=self.global_ceiling_usd,
                        reason="instance_cap_exceeded",
                    )

                if global_committed + estimated_max_usd > self.global_ceiling_usd:
                    self._conn.execute("COMMIT")
                    raise BudgetRefused(
                        instance_id=instance_id,
                        requested_usd=estimated_max_usd,
                        settled_usd=global_settled,
                        outstanding_usd=global_outstanding,
                        cap_usd=instance_cap,
                        ceiling_usd=self.global_ceiling_usd,
                        reason="global_ceiling_exceeded",
                    )

                # 5. Insert open reservation
                res_id = new_id("lres")
                expires_at = now + (ttl_seconds if ttl_seconds is not None else self.ttl_seconds)
                self._conn.execute(
                    """
                    INSERT INTO loop_reservations
                    (id, instance_id, capability_id, max_usd, state, created_at, expires_at, attempts, actual_usd, cost_quality)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        res_id,
                        instance_id,
                        capability_id,
                        estimated_max_usd,
                        STATE_OPEN,
                        now,
                        expires_at,
                        0,
                        None,
                        "unknown",
                    ),
                )

                self._conn.execute("COMMIT")

                return Reservation(
                    id=res_id,
                    instance_id=instance_id,
                    capability_id=capability_id,
                    max_usd=estimated_max_usd,
                    state=STATE_OPEN,
                    created_at=now,
                    expires_at=expires_at,
                    attempts=0,
                    actual_usd=None,
                    cost_quality="unknown",
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
        cost_quality: str = "unknown",
        usage_available: bool = True,
    ) -> Reservation:
        """Convert held reservation into settled spend (real cost recorded).

        FAIL CLOSED: if actual_usd is None, NaN, infinite, negative, or
        usage_available is False, charge the FULL reserved max_usd.

        Args:
            reservation_id: The reservation to settle.
            actual_usd: Real cost from provider. None means use max_usd.
            cost_quality: One of "exact", "estimated", "unknown".
            usage_available: True if provider reported usage.

        Returns:
            The settled Reservation.

        Raises:
            BudgetError: Reservation not found or not open.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM loop_reservations WHERE id = ?", (reservation_id,)
                ).fetchone()
                if not row:
                    raise BudgetError(f"Reservation {reservation_id} not found")

                res = self._row_to_reservation(row)
                if res.state == STATE_SETTLED:
                    self._conn.execute("COMMIT")
                    return res

                if res.state == STATE_EXPIRED_UNKNOWN:
                    # Already charged at its maximum by the sweep. Terminal on
                    # purpose: a late settle would lower a charge the ledger
                    # made precisely because it could not tell whether the
                    # effect happened, and "the caller came back eventually"
                    # is not new evidence about the provider.
                    raise BudgetError(
                        f"Reservation {reservation_id} expired unaccounted and was already "
                        f"charged at its {res.max_usd:.4f} USD maximum; it cannot be settled"
                    )

                if res.state != STATE_OPEN:
                    raise BudgetError(
                        f"Reservation {reservation_id} is in state {res.state!r}, cannot settle"
                    )

                is_valid_actual = True
                if actual_usd is None or not usage_available:
                    is_valid_actual = False
                else:
                    try:
                        val = float(actual_usd)
                        if math.isnan(val) or not math.isfinite(val) or val < 0.0:
                            is_valid_actual = False
                    except (ValueError, TypeError):
                        is_valid_actual = False

                if not is_valid_actual:
                    resolved_usd = res.max_usd
                    resolved_quality = "unknown"
                else:
                    assert actual_usd is not None
                    resolved_usd = max(0.0, float(actual_usd))
                    resolved_quality = cost_quality

                self._conn.execute(
                    f"UPDATE loop_reservations SET state = '{STATE_SETTLED}', actual_usd = ?, cost_quality = ? WHERE id = ?",  # noqa: S608
                    (resolved_usd, resolved_quality, reservation_id),
                )

                updated_row = self._conn.execute(
                    "SELECT * FROM loop_reservations WHERE id = ?", (reservation_id,)
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

    def release(self, reservation_id: str) -> Reservation:
        """Release a held reservation (call PROVABLY never happened, freed the hold).

        Idempotent. Charges nothing — which is why the caller must be able to
        show that no provider was reached (``SeamUnavailable``, or a refusal
        decided locally before the request was built). Anything ambiguous
        settles instead; see ``loop_effects.execute``'s ``_dispose_reservation``.

        A reservation the sweep has already charged is terminal and cannot be
        released back to zero.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM loop_reservations WHERE id = ?", (reservation_id,)
                ).fetchone()
                if not row:
                    raise BudgetError(f"Reservation {reservation_id} not found")

                res = self._row_to_reservation(row)
                if res.state == STATE_RELEASED:
                    self._conn.execute("COMMIT")
                    return res

                if res.state == STATE_EXPIRED_UNKNOWN:
                    raise BudgetError(
                        f"Reservation {reservation_id} expired unaccounted and was charged at "
                        f"its {res.max_usd:.4f} USD maximum; it cannot be released to zero"
                    )

                if res.state != STATE_OPEN:
                    raise BudgetError(
                        f"Reservation {reservation_id} is in state {res.state!r}, cannot release"
                    )

                self._conn.execute(
                    f"UPDATE loop_reservations SET state = '{STATE_RELEASED}', actual_usd = 0.0 WHERE id = ?",  # noqa: S608
                    (reservation_id,),
                )

                updated_row = self._conn.execute(
                    "SELECT * FROM loop_reservations WHERE id = ?", (reservation_id,)
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

    def reclaim_expired(self, *, now: float | None = None) -> list[str]:
        """Sweep: open reservations past ``expires_at`` are CHARGED, not freed.

        An expiry means the owner never came back — a hard kill between the
        reserve and the settle, or a ``settle()`` that itself failed. That says
        nothing about whether the provider ran and billed, so the reservation
        settles at its full reserved maximum with ``cost_quality='unknown'``
        and keeps counting against the day's caps. See
        :data:`STATE_EXPIRED_UNKNOWN` for why charging beats freeing here.

        Idempotent, and loud: every reservation it charges is logged at WARNING
        and handed to ``notifier`` when one is configured.
        """
        if now is None:
            now = self.clock()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                rows = self._conn.execute(
                    f"SELECT * FROM loop_reservations WHERE state = '{STATE_OPEN}' AND expires_at <= ?",  # noqa: S608
                    (now,),
                ).fetchall()
                reclaimed = [self._row_to_reservation(row) for row in rows]
                if reclaimed:
                    self._conn.execute(
                        f"""
                        UPDATE loop_reservations
                        SET state = '{STATE_EXPIRED_UNKNOWN}',
                            actual_usd = max_usd,
                            cost_quality = 'unknown'
                        WHERE state = '{STATE_OPEN}' AND expires_at <= ?
                        """,  # noqa: S608 — the interpolations are module constants
                        (now,),
                    )
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

        # Alarms fire AFTER the commit and outside the transaction: a notifier
        # is operator-supplied code and must never be able to wedge or roll
        # back the ledger. Nothing here is allowed to raise, because the charge
        # is already durable and losing the alarm must not lose the sweep.
        for reservation in reclaimed:
            logger.warning(
                "loop budget: reservation %s (instance=%s capability=%s) expired without "
                "settling after %.0fs; CHARGING its %.4f USD maximum as unaccounted spend — "
                "the effect may have executed and been billed",
                reservation.id,
                reservation.instance_id,
                reservation.capability_id,
                max(0.0, now - reservation.created_at),
                reservation.max_usd,
            )
            if self.notifier is not None:
                with contextlib.suppress(Exception):
                    self.notifier(
                        reservation.instance_id,
                        "alarm",
                        "reservation_expired_unaccounted",
                        reservation.max_usd,
                    )
        return [reservation.id for reservation in reclaimed]

    def get_instance_state(
        self, instance_id: str, *, now: float | None = None, sweep: bool = True
    ) -> InstanceState:
        """Get current spend state for an instance on the current UTC day.

        .. warning::

           **This read WRITES by default.** ``sweep=True`` calls
           :meth:`reclaim_expired`, which transitions expired holds to
           ``expired_unknown``, logs at WARNING and fires ``notifier``. That is
           correct for an operator report and wrong for a poller: wire this into
           a dashboard that refreshes every few seconds and you have made a
           display loop the thing that drives billing state and pages people.
           Pass ``sweep=False`` for any caller that must not have side effects.

        Reports the same numbers ``reserve`` admits against, including any
        expired-unknown charges, and breaks the unaccounted part out separately
        so an operator can see that something died mid-effect.

        It sweeps by default for that reason: a hold whose TTL has run out is
        not an in-flight call, and reporting it as ``outstanding`` would tell an
        operator a worker is still working when the ledger has already decided
        nobody is coming back. The charge is determined by the clock either
        way; the sweep only writes down what is already true.

        ``sweep=False`` cannot make a caller UNDER-count, and that is a property
        of the schema rather than a promise: an expired-but-unswept hold is
        still ``open``, so ``_OUTSTANDING_SQL`` counts it at ``max_usd``, and
        the sweep settles it at exactly ``actual_usd = max_usd`` into a charged
        state. The same dollars are committed either way; only the BUCKET
        changes, from ``outstanding`` to ``settled``/``unaccounted``. So what
        ``sweep=False`` costs is the attribution — a dead hold still reads as
        in-flight work — and never the total.

        ``reserve`` sweeps on its own account regardless of this flag, so a
        reader passing ``sweep=False`` cannot cause an expired hold to be spent
        twice; the admission gate is not relying on anyone else to sweep for it.
        """
        if now is None:
            now = self.clock()
        day_start = _utc_day_start(now)
        if sweep:
            self.reclaim_expired(now=now)

        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT
                    SUM({_SPENT_SQL}) as settled,
                    SUM({_OUTSTANDING_SQL}) as outstanding,
                    SUM({_UNACCOUNTED_SQL}) as unaccounted
                FROM loop_reservations
                WHERE instance_id = ? AND created_at >= ?
                """,  # noqa: S608 — the interpolations are module constants
                (instance_id, day_start),
            ).fetchone()

            settled = row["settled"] if row and row["settled"] else 0.0
            outstanding = row["outstanding"] if row and row["outstanding"] else 0.0
            unaccounted = row["unaccounted"] if row and row["unaccounted"] else 0.0
            committed = settled + outstanding
            cap = self._get_instance_cap(instance_id)
            available = max(0.0, cap - committed)
            fraction = committed / cap if cap > 0.0 else 0.0

            return InstanceState(
                instance_id=instance_id,
                cap_usd=cap,
                settled_usd=settled,
                outstanding_usd=outstanding,
                committed_usd=committed,
                available_usd=available,
                fraction_used=fraction,
                unaccounted_usd=unaccounted,
            )

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            if self._conn:
                with contextlib.suppress(Exception):
                    self._conn.close()


__all__ = [
    "BudgetError",
    "BudgetRefused",
    "DEFAULT_INSTANCE_CAP_USD",
    "GLOBAL_DAILY_CEILING_USD",
    "INSTANCE_CAPS",
    "STATE_EXPIRED_UNKNOWN",
    "STATE_OPEN",
    "STATE_RELEASED",
    "STATE_SETTLED",
    "InstanceState",
    "LoopBudgetLedger",
    "Reservation",
    "UnknownCostRefused",
]
