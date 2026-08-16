"""SwarmDal: dedicated SQLite DAL for swarm runs, deps, and attempts (WP1).

OrchestrationsDal's defensive connection discipline (own connection, WAL,
busy_timeout, RLock) with the longhaul store's transactional idioms: every
multi-statement write is one BEGIN IMMEDIATE; attempt close is an idempotent
CAS on ``ended_at IS NULL``; ``open_attempt`` refuses terminal/archived tasks
and second live attempts inside its own transaction (the 043 idiom, backed by
the ``idx_swarm_attempts_live`` partial unique index at the SQL layer).

Board membership invariants enforced here:
- ``lane`` is the execution-ownership axis and is NEVER written by swarm code
  (the 043 CHECK is frozen; swarm membership is ``swarm_run_id`` only);
- ``category_id`` is pure cross-lane metadata (the board taxonomy): a swarm
  card may carry one. Longhaul WIP counting is lane-scoped
  (``lane='longhaul'`` only, see ``LonghaulStore.claim_category_slot``), so a
  categorized swarm card never consumes a longhaul slot.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any

from omniagentos.contracts import CostObservation, new_id, utc_now_iso
from omniagentos.db.busy import (
    BusyRetryPolicy,
    execute_write_transaction,
    run_with_busy_retry,
)
from omniagentos.db.store import (
    _aggregate_provider_call_cost_rows,
    _get_provider_call_row,
    _provider_call_where,
    _record_provider_call_row,
    _settle_provider_call_row,
)
from omniagentos.northstar.collectors import (
    AllocationMeasurement,
    record_allocation_frontier_report,
    record_phase_baseline,
    record_provisioning_latency,
    record_rediscovery_event,
    record_tool_set_digest,
)
from omniagentos.scope.config import scope_locks_enabled
from omniagentos.scope.locks import PathLockStore
from omniagentos.swarm.contracts import (
    ATTEMPT_END_REASONS,
    SWARM_EVENT_KIND,
    TERMINAL_RUN_STATUSES,
    SwarmRunStatus,
)

_TERMINAL_STATUSES = frozenset(str(status) for status in TERMINAL_RUN_STATUSES)
_ACTIVE_STATUSES = ("planning", "running", "merging")
_TERMINAL_TASK_STATUSES = ("done", "blocked", "cancelled")

# HANDOFF/L14-M33 adoption. The ad-hoc backoff LOOP that used to live in
# ``_serialized`` is retired in favour of the shared ``omniagentos.db.busy``
# primitive, but this dal's *attempt budget* is a behavioural contract, not an
# implementation detail: B05's lease/busy controls are specified and tested at
# FIVE attempts (tests/swarm/test_h35_m34_m35_m44.py). Adopting the seam must
# not silently widen that budget to ``DEFAULT_BUSY_RETRY_POLICY``'s ten — a
# scheduler that waits twice as long before surfacing contention is a different
# system. So the old constants are preserved as an explicit policy object and
# passed to BOTH retry entry points (``_serialized`` and :meth:`SwarmDal._write_txn`),
# which keeps every write method on one budget instead of splitting it between
# adopted (10) and unadopted (5) methods. Jitter is the one deliberate change:
# the old fixed 0.05·2ⁿ backoff synchronises concurrent writers on the same
# retry instants, which is the thundering-herd shape the shared policy exists
# to avoid. Contention is now observable in ``BUSY_RETRY_METRICS`` for every
# adopter instead of vanishing into this dal's private loop.
_SWARM_DAL_BUSY_POLICY = BusyRetryPolicy(
    max_attempts=5,
    base_delay_ms=50.0,
    max_delay_ms=500.0,
    jitter_ms=50.0,
)

# The rows whose session token-estimate (migration 118) is the only price
# available: a tokens-only usage marker, or an attempt with no dollar figure
# whose session has none either. Shared VERBATIM by every cap-accrual query so
# run-wide spend and per-card spend can never disagree about what is priced.
_ESTIMATE_IS_LIVE_SQL = (
    "s.id IS NOT NULL AND s.cost_estimate_usd IS NOT NULL AND ("
    "  a.usage_source = 'cli-report-tokens-only' "
    "  OR (a.cost_usd IS NULL AND ("
    "    s.usage_source = 'cli-report-tokens-only' OR s.cost_usd IS NULL"
    "  ))"
    ")"
)
# The per-attempt dollars a cap should count, mirroring
# ``RunBudgetSpend.known_cost_usd``: the attempt's own price wins over its
# session backfill, and a tokens-only marker contributes nothing here because
# it is priced by ``_ESTIMATE_IS_LIVE_SQL`` instead.
_KNOWN_ATTEMPT_COST_SQL = (
    "CASE "
    "  WHEN a.usage_source = 'cli-report-tokens-only' THEN 0.0 "
    "  WHEN a.cost_usd IS NOT NULL THEN a.cost_usd "
    "  WHEN s.usage_source = 'cli-report-tokens-only' THEN 0.0 "
    "  ELSE COALESCE(s.cost_usd, 0.0) "
    "END"
)
# The card-level key a split stamps on each child at admission: its slice of
# the entitlement its parent held. Lives in ``board_tasks.swarm_json`` on
# purpose -- the split transaction writes it with the children it funds, and
# the transaction that terminalizes a card releases it, so there is no separate
# reservation table to fall out of step with the DAG.
BUDGET_RESERVED_KEY = "budget_reserved_usd"


@dataclass(frozen=True)
class RunBudgetSpend:
    """The best dollar-budget observation available for one swarm run.

    ``known_cost_usd`` is a lower bound when ``unknown_cost_sessions`` is
    non-zero. ``cost_usd`` deliberately becomes ``None`` in that case: an
    incomplete total must never be compared to a cap as though missing prices
    were free.

    ``estimated_cost_usd`` (C4) is the OTHER half of that truth: an upper-bound
    price for the tokens those unpriced sessions actually measured, taken from
    ``sessions.cost_estimate_usd`` (migration 118). It is never folded into
    ``known_cost_usd`` and never makes ``cost_usd`` non-None -- an unknown total
    stays unknown. It exists so a dollar CAP can still accrue token spend
    instead of reading zero forever; see :attr:`accrued_cost_usd`.
    """

    known_cost_usd: float
    unknown_cost_sessions: int
    known_usd_decimal: str | None = None
    estimated_upper_bound_usd: float | None = None
    chargeable_usd: float | None = None
    unknown_call_count: int = 0
    quality: str = "unknown"
    safe_to_compare: bool = False
    accounting_incomplete: bool = False
    estimated_cost_usd: float = 0.0
    estimated_cost_sessions: int = 0

    @property
    def cost_usd(self) -> float | None:
        if self.accounting_incomplete or self.unknown_cost_sessions or self.unknown_call_count:
            return None
        if self.chargeable_usd is not None:
            return self.chargeable_usd
        return self.known_cost_usd

    @property
    def accrued_cost_usd(self) -> float:
        """What a dollar CAP should count: measured dollars + priced tokens.

        Deliberately NOT a truth-telling total -- it mixes an exact figure with
        an upper-bound estimate, so it is only ever compared to a ceiling, never
        reported as spend. ``cost_usd`` remains the honest answer.
        """
        return self.known_cost_usd + self.estimated_cost_usd


@dataclass(frozen=True)
class ProjectBudgetSpend:
    """The aggregate budget observation for every swarm run in one project.

    ``known_cost_usd`` is only a lower bound when either unknown counter is
    non-zero.  Consumers must use ``cost_usd`` or ``safe_to_compare`` instead
    of coercing an incomplete total to zero.

    ``estimated_cost_usd`` mirrors :class:`RunBudgetSpend`: priced tokens from
    unpriced sessions, for cap accrual only.  It never makes an unknown project
    total look known -- M4's "unknown spend is unenforceable, never zero" rule
    is unchanged; the estimate only adds a SECOND way for a cap to be reached.
    """

    project_id: str
    budget_usd: float | None
    known_cost_usd: float
    unknown_cost_sessions: int
    unknown_call_count: int = 0
    estimated_cost_usd: float = 0.0
    estimated_cost_sessions: int = 0

    @property
    def safe_to_compare(self) -> bool:
        return not self.unknown_cost_sessions and not self.unknown_call_count

    @property
    def cost_usd(self) -> float | None:
        return self.known_cost_usd if self.safe_to_compare else None

    @property
    def accrued_cost_usd(self) -> float:
        """Measured dollars + priced tokens, for cap comparison only."""
        return self.known_cost_usd + self.estimated_cost_usd


def _serialized(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: acquire the store lock, retry the whole call on SQLITE_BUSY/LOCKED.

    HANDOFF/L14-M33 adoption: the bounded backoff used to be a private 5-attempt
    loop; it now runs through ``omniagentos.db.busy.run_with_busy_retry`` --
    the module's documented alternate to ``execute_write_transaction`` for
    callers that manage their own BEGIN/COMMIT/ROLLBACK ("the callable must
    BEGIN / COMMIT / ROLLBACK itself", see ``busy.py``'s module docstring).
    Every ``SwarmDal`` write method does exactly that via
    ``self._begin``/``self._commit``/``self._rollback`` (or, for the methods
    converted onto :meth:`SwarmDal._write_txn`, via
    ``execute_write_transaction`` directly), so re-invoking ``func`` from
    scratch on a retry is transaction-safe. Retrying under ``self._lock``
    keeps this single-connection dal serialized exactly as before; the whole
    method (read prep + write) is the retried unit, matching the previous
    behaviour, and ``_SWARM_DAL_BUSY_POLICY`` preserves the previous FIVE-attempt
    budget rather than inheriting the shared default's ten.

    The lock is held ACROSS the backoff sleeps, exactly as the old private loop
    held it (``with self._lock:`` wrapped the whole ``for`` loop). That is why no
    ``lock=`` is handed to the retry primitive: this dal owns a single
    connection, so releasing the lock between attempts would let a second
    thread open a transaction on that same connection mid-retry. The
    "acquire inside the attempt" advice in ``busy.py`` is for stores whose lock
    guards a connection they are willing to yield; this one is not.
    """

    @wraps(func)
    def wrapper(self: SwarmDal, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return run_with_busy_retry(
                lambda: func(self, *args, **kwargs),
                policy=_SWARM_DAL_BUSY_POLICY,
                op=f"swarm_dal.{func.__name__}",
            )

    return wrapper


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _normalized_project_id(value: Any) -> str | None:
    """The project a row is scoped to, with blank collapsed to ``None``.

    M1. Blank is NULL, for the same reason migration 115 gives on the grant
    side: ``project_id = ''`` would be a project id that no project has, and
    the broker's ``_row_project`` already collapses it — so a write path that
    persisted ``''`` would record "scoped to nothing", a third state between
    "unscoped" and "scoped to project P" that nothing downstream can act on.
    ``None`` means exactly one thing: this row proves no project binding.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# --- pump ledger identity (109) -------------------------------------------------
#
# A namespace, NOT a run: no `swarm_runs` row is ever inserted under this id.
# See `SwarmDal.ensure_pump_lane` for why.
PUMP_LEDGER_RUN_ID = "swr_pumpledger"

_PUMP_SLUG_RE = re.compile(r"[^a-z0-9]+")


def pump_lane_task_id(lane: str) -> str:
    """Deterministic board-card id for a pump lane.

    Pure function of the lane name, so "ensure" is idempotent across processes
    and across restarts without a lookup table. The readable slug is for the
    operator; the digest suffix is what makes it collision-free (``p1/cost`` and
    ``p1-cost`` slugify identically, and two lanes sharing one ledger card would
    make the retry cap fire on the wrong lane).
    """
    lane = str(lane).strip()
    slug = _PUMP_SLUG_RE.sub("_", lane.lower()).strip("_")[:32] or "lane"
    digest = hashlib.sha256(lane.encode("utf-8")).hexdigest()[:8]
    return f"btk_pump_{slug}_{digest}"


def pump_verdict_hash(body: str) -> str:
    """The loop-progress fingerprint: sha256 of the body a dispatch reacts to.

    Trailing whitespace is stripped so that a verdict rewritten byte-identically
    apart from a newline still reads as "no new information". Nothing else is
    normalised — two verdicts that differ by one cited file:line ARE different
    information and must be allowed to retry.
    """
    return hashlib.sha256(str(body).strip().encode("utf-8", "replace")).hexdigest()


class SwarmDal:
    """A dedicated SQLite connection for swarm run/dep/attempt state."""

    def __init__(self, db_path: str | Path) -> None:
        self._lock = RLock()
        # This dal is SINGLE-CONNECTION: one connection shared by every thread,
        # guarded by one RLock. ``SqliteStore`` splits readers onto per-thread
        # connections behind a separate ``_read_lock``; there is nothing to split
        # here, so the reader guard IS the writer lock. Both names exist so this
        # dal structurally satisfies ``scope.locks._WriterStore`` (see
        # :attr:`path_locks`) instead of relying on that module's getattr
        # fallbacks — and ``_shared_connection`` is not a white lie: it means
        # exactly "one connection, no per-thread readers to spread over", which
        # is what this is.
        self._read_lock = self._lock
        path = str(Path(db_path).expanduser()) if str(db_path) != ":memory:" else ":memory:"
        self._db_path = path
        self._connection = sqlite3.connect(
            path, isolation_level=None, timeout=5.0, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._path_locks: PathLockStore | None = None
        self._attempt_close_hooks: list[Callable[[dict[str, Any]], None]] = []

    @property
    def _shared_connection(self) -> sqlite3.Connection:
        """The one connection every thread uses (see ``__init__``)."""
        return self._connection

    @property
    def path_locks(self) -> PathLockStore:
        """The cross-lane scope-lock store bound to THIS dal's connection.

        Deliberately NOT a second connection: the lock table's whole safety
        argument is one ``BEGIN IMMEDIATE`` per acquire, and a private connection
        would let this dal open two overlapping transactions on two connections
        against the same database.

        CALL IT FROM OUTSIDE THIS DAL'S OWN TRANSACTIONS. ``PathLockStore._write``
        issues its own ``BEGIN IMMEDIATE``; ``self._lock`` is an RLock, so calling
        a lock method from inside a ``self._begin()`` block re-enters the lock
        happily and then dies on "cannot start a transaction within a
        transaction" — a failure that only shows up under load. That is why
        :meth:`adopt_run` re-keys lock rows with inline SQL rather than through
        this store.
        """
        store = self._path_locks
        if store is None:
            store = PathLockStore(self)
            self._path_locks = store
        return store

    @property
    def db_path(self) -> str:
        """The resolved sqlite file this dal is bound to.

        Exposed (read-only) so sibling swarm routes that must read the SAME
        database through a *different* helper — e.g. ``GET /api/swarm/providers``
        reading ``claude_accounts`` + durable inflight via
        ``omniagentos.routing.limit_state`` — can target the dal's database
        (the test-injected tmp file, or ``default_db_path()`` in prod) instead
        of always resolving ``default_db_path()`` themselves."""
        return self._db_path

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # --- North Star measurement carriers ------------------------------------

    @_serialized
    def write_northstar_rediscovery_event(self, **event: Any) -> int:
        """Write a context-rediscovery trace on this DAL's durable connection."""
        return record_rediscovery_event(self._connection, **event)

    @_serialized
    def write_northstar_provisioning_latency(self, **event: Any) -> int:
        """Write a measured provisioning interval."""
        return record_provisioning_latency(self._connection, **event)

    @_serialized
    def write_northstar_phase_baseline(
        self, *, run_id: str, baseline_key: str, anchors: dict[str, float]
    ) -> int:
        """Write a complete North Star phase baseline."""
        return record_phase_baseline(
            self._connection, run_id=run_id, baseline_key=baseline_key, anchors=anchors
        )

    @_serialized
    def write_northstar_allocation_frontier_report(
        self,
        *,
        report_key: str,
        configurations: Sequence[AllocationMeasurement],
        created_at: str,
    ) -> int:
        """Write an auditable allocation-frontier report."""
        return record_allocation_frontier_report(
            self._connection,
            report_key=report_key,
            configurations=configurations,
            created_at=created_at,
        )

    @_serialized
    def write_northstar_tool_set_digest(
        self, *, attempt_id: str, tool_names: Sequence[str]
    ) -> str:
        """Write the canonical tool-set identity for one attempt."""
        return record_tool_set_digest(
            self._connection, attempt_id=attempt_id, tool_names=tool_names
        )

    def _begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._connection.commit()

    def _rollback(self) -> None:
        self._connection.rollback()

    def _write_txn(self, body: Callable[[sqlite3.Connection], Any], *, op: str) -> Any:
        """Run one single-connection write transaction through the shared
        HANDOFF/L14-M33 seam (``omniagentos.db.busy.execute_write_transaction``).

        ``body`` receives ``self._connection`` (this dal is single-connection,
        see :attr:`_shared_connection`) and must be the WHOLE unit of work --
        the same "retry from scratch" rule ``_serialized`` already enforces at
        the method level. No ``lock=`` is passed here: every caller already
        runs inside ``@_serialized``, which holds ``self._lock`` (an RLock) for
        the whole call, so a second acquire would be a harmless but pointless
        re-entry.

        RE-ENTRANCY GUARD. The methods this replaced opened their transaction
        with ``self._begin()``, which raises "cannot start a transaction within
        a transaction" if one is already open — a loud failure that has kept
        callers honest (see the same rule spelled out in
        ``sessions/scope_wiring.py``'s ``SessionScopeGate`` docstring). The
        shared seam instead ``rollback()``s any residual transaction before its
        ``BEGIN``, which for a *nested* call would silently DISCARD the outer
        unit of work. Swapping a crash for silent data loss is not an
        acceptable side effect of adopting a retry seam, so the pre-adoption
        failure mode is reproduced here explicitly, with the original exception
        type and message. ``self._lock`` being an RLock means a nested call
        cannot be caught by the lock — only by this check.

        The attempt budget is ``_SWARM_DAL_BUSY_POLICY``, the same one
        ``_serialized`` uses, so a converted method and an unconverted one get
        identical contention behaviour. Exhaustion raises ``BusyRetryExhausted``,
        which ``busy.py`` deliberately excludes from ``is_busy_or_locked``, so
        the enclosing ``_serialized`` retry does NOT multiply the budget.
        """
        if self._connection.in_transaction:
            raise sqlite3.OperationalError(
                "cannot start a transaction within a transaction"
            )
        return execute_write_transaction(
            self._connection, body, policy=_SWARM_DAL_BUSY_POLICY, op=op
        )

    # --- runs -----------------------------------------------------------------

    @_serialized
    def create_run(
        self,
        *,
        working_dir: str,
        goal: str,
        board_task_id: str | None = None,
        project_id: str | None = None,
        status: str = "queued",
        plan: dict[str, Any] | None = None,
        target_concurrency: int = 1,
        max_concurrency: int = 10,
        budget_usd_max: float | None = None,
        source: str = "real",
    ) -> dict[str, Any]:
        """Insert a swarm run and return the full row."""
        if status not in {str(value) for value in SwarmRunStatus}:
            raise ValueError(f"invalid swarm run status: {status!r}")
        run_id = new_id("swr")
        now = utc_now_iso()
        plan_json = json.dumps(plan or {}, separators=(",", ":"), sort_keys=True)

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO swarm_runs "
                "(id, board_task_id, project_id, working_dir, goal, status, plan_json, "
                "target_concurrency, max_concurrency, budget_usd_max, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    board_task_id,
                    _normalized_project_id(project_id),
                    working_dir,
                    goal,
                    status,
                    plan_json,
                    target_concurrency,
                    max_concurrency,
                    budget_usd_max,
                    source,
                    now,
                    now,
                ),
            )

        self._write_txn(_write, op="swarm_dal.create_run")
        row = self._connection.execute(
            "SELECT * FROM swarm_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return dict(row)

    @_serialized
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM swarm_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return None if row is None else dict(row)

    @_serialized
    def list_runs(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is None:
            return _rows(
                self._connection.execute(
                    "SELECT * FROM swarm_runs ORDER BY created_at ASC, rowid ASC"
                ).fetchall()
            )
        return _rows(
            self._connection.execute(
                "SELECT * FROM swarm_runs WHERE status = ? ORDER BY created_at ASC, rowid ASC",
                (status,),
            ).fetchall()
        )

    @_serialized
    def finished_runs_since(
        self, since_finished_at: str | None, since_rowid: int = 0
    ) -> list[dict[str, Any]]:
        """Terminal ``swarm_runs`` strictly after the ``(finished_at, rowid)``
        watermark pair, oldest first — WP8 optimizer's incremental read.

        ``rowid`` (SQLite's implicit, monotonically-increasing insertion order)
        breaks ties when two runs share the same second-granularity
        ``finished_at``, so a batch boundary can never silently strand a run
        at the tie point. ``since_finished_at=None`` means "from the
        beginning" (first-ever pass, or a deleted/reset watermark file).
        Each row carries its own ``_rowid`` so the caller can persist the next
        watermark without a second query.
        """
        if since_finished_at:
            rows = self._connection.execute(
                "SELECT *, rowid AS _rowid FROM swarm_runs "
                "WHERE finished_at IS NOT NULL "
                "AND (finished_at > ? OR (finished_at = ? AND rowid > ?)) "
                "ORDER BY finished_at ASC, rowid ASC",
                (since_finished_at, since_finished_at, since_rowid),
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT *, rowid AS _rowid FROM swarm_runs WHERE finished_at IS NOT NULL "
                "ORDER BY finished_at ASC, rowid ASC"
            ).fetchall()
        return _rows(rows)

    @_serialized
    def set_run_status(self, run_id: str, status: str, *, error: str | None = None) -> bool:
        """Advance a run's lifecycle; stamps started_at/finished_at edges."""
        if status not in {str(value) for value in SwarmRunStatus}:
            raise ValueError(f"invalid swarm run status: {status!r}")
        now = utc_now_iso()
        assignments = ["status = ?", "heartbeat_at = ?", "updated_at = ?"]
        values: list[Any] = [status, now, now]
        if error is not None or status == "completed":
            assignments.append("error = ?")
            values.append(error)
        if status != "queued":
            assignments.append("started_at = COALESCE(started_at, ?)")
            values.append(now)
        if status in _TERMINAL_STATUSES:
            assignments.append("finished_at = ?")
            values.append(now)
        values.append(run_id)
        self._begin()
        try:
            cursor = self._connection.execute(
                f"UPDATE swarm_runs SET {', '.join(assignments)} WHERE id = ?", values
            )
            self._commit()
            changed = cursor.rowcount == 1
        except BaseException:
            self._rollback()
            raise
        # A run that has stopped must not leave live-looking cards behind. The
        # commit above is the ONE chokepoint every terminal transition passes
        # through (scheduler _complete_run/_fail_run, the cancel route, resume),
        # so the close-out belongs here rather than at each call site. Runs the
        # RLock re-entrantly but strictly AFTER the commit — never nested inside
        # this transaction.
        if changed and status in _TERMINAL_STATUSES:
            self.close_out_run_cards(run_id, run_status=status)
        return changed

    @_serialized
    def cancel_run_if_active(self, run_id: str, *, error: str | None = None) -> bool:
        """Atomically flip a NON-terminal run to ``cancelled`` — a real CAS.

        ``UPDATE ... WHERE id = ? AND status NOT IN (<terminal>)``: the
        status predicate and the write are one statement, so of N concurrent
        cancels exactly ONE observes ``rowcount == 1`` and returns True — that
        caller owns the irreversible side effects (the ``run_failed`` receipt,
        the card close-out). Every loser returns False without touching the
        row. ``set_run_status`` deliberately has no such guard (the scheduler
        uses it for forward transitions it already owns); cancel needs one
        because any number of operators can press Stop at once.

        Same edge stamps as ``set_run_status``: heartbeat/updated always,
        ``started_at`` back-filled if the run never started, ``finished_at``
        because ``cancelled`` is terminal.
        """
        now = utc_now_iso()
        terminal = sorted(_TERMINAL_STATUSES)
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE swarm_runs SET status = 'cancelled', error = ?, "
                "heartbeat_at = ?, updated_at = ?, "
                "started_at = COALESCE(started_at, ?), finished_at = ? "
                f"WHERE id = ? AND status NOT IN ({', '.join('?' for _ in terminal)})",
                (error, now, now, now, now, run_id, *terminal),
            )
            self._commit()
            won = cursor.rowcount == 1
        except BaseException:
            self._rollback()
            raise
        # Mirrors set_run_status: card close-out belongs to whichever call
        # actually performed the terminal transition, after the commit.
        if won:
            self.close_out_run_cards(run_id, run_status="cancelled")
        return won

    @_serialized
    def close_out_run_cards(self, run_id: str, *, run_status: str) -> list[str]:
        """Settle a terminal run's still-live board cards. Returns the ids flipped.

        Without this, a run that failed or was cancelled left its root card
        (``Swarm: <goal>``) and every unfinished member card sitting at
        ``in_progress``/``open`` FOREVER: the board showed permanently
        "Running" work that no coordinator would ever advance, and the operator
        could not tell a wedged run from a live one. Only ``_complete_run``
        closed its root card, so the whole failed/cancelled half of the
        lifecycle leaked (17 stranded cards across 6 dead runs when this was
        found).

        The landing status is chosen so the board stays HONEST:

        - run ``cancelled``  -> cards ``cancelled``  (deliberately stopped)
        - run ``failed``     -> cards ``blocked``    (died mid-flight; a blocked
          card is the one state the UI offers Resume on, which is exactly the
          right affordance)
        - run ``completed``  -> leftovers ``cancelled`` (the DAG settled without
          them; they will never run)

        Cards already terminal (done/blocked/cancelled) and archived cards are
        left alone, so this is idempotent and safe to re-run over old data.
        ``claimed_by`` is cleared because no agent owns the card any more.
        """
        landing = "blocked" if run_status == "failed" else "cancelled"
        now = utc_now_iso()
        placeholders = ", ".join("?" for _ in _TERMINAL_TASK_STATUSES)
        self._begin()
        try:
            rows = self._connection.execute(
                "UPDATE board_tasks SET status = ?, claimed_by = NULL, updated_at = ? "
                "WHERE swarm_run_id = ? AND archived_at IS NULL "
                f"AND status NOT IN ({placeholders}) RETURNING id",
                (landing, now, run_id, *_TERMINAL_TASK_STATUSES),
            ).fetchall()
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return [str(row["id"]) for row in rows]

    @_serialized
    def heartbeat(self, run_id: str, *, generation: int | None = None) -> bool:
        """Coordinator lease renewal — the takeover signal for resume_swarm.

        m10 fencing: with ``generation`` the renewal is CONDITIONAL on the
        row's ``lease_generation`` still matching — ``adopt_run`` bumps it,
        so a displaced coordinator's beat returns False (rowcount 0) and the
        caller must stop coordinating. Without ``generation`` the legacy
        unconditional renewal is kept for callers outside the fenced loop."""
        now = utc_now_iso()
        if generation is None:
            cursor = self._connection.execute(
                "UPDATE swarm_runs SET heartbeat_at = ?, updated_at = ? WHERE id = ?",
                (now, now, run_id),
            )
        else:
            cursor = self._connection.execute(
                "UPDATE swarm_runs SET heartbeat_at = ?, updated_at = ? "
                "WHERE id = ? AND lease_generation = ?",
                (now, now, run_id, int(generation)),
            )
        return cursor.rowcount == 1

    @_serialized
    def mark_stale_failed(self, *, stale_minutes: int) -> list[dict[str, Any]]:
        """Fail ACTIVE runs whose coordinator heartbeat went stale.

        Queued runs are excluded on purpose: admission control parks them with
        no coordinator, so a stale heartbeat there is normal, not a death.
        """
        cutoff = (datetime.now(UTC) - timedelta(minutes=max(0, stale_minutes))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        now = utc_now_iso()
        self._begin()
        try:
            rows = self._connection.execute(
                "UPDATE swarm_runs SET status = 'failed', "
                "error = 'stale heartbeat — swarm coordinator died', "
                "finished_at = ?, updated_at = ? "
                "WHERE status IN (?, ?, ?) AND heartbeat_at < ? RETURNING *",
                (now, now, *_ACTIVE_STATUSES, cutoff),
            ).fetchall()
            self._commit()
        except BaseException:
            self._rollback()
            raise
        failed = _rows(rows)
        # This sweep writes swarm_runs directly rather than going through
        # set_run_status, so it must settle the dead runs' cards itself — a
        # coordinator that died is precisely the case that used to leave
        # permanently "Running" cards on the board.
        for run in failed:
            self.close_out_run_cards(str(run["id"]), run_status="failed")
        return failed

    @_serialized
    def try_activate_run(self, run_id: str) -> bool:
        """Claim-before-act activation CAS (two-process discipline).

        Flips ``queued``/``planning`` → ``running`` and stamps the coordinator
        heartbeat in ONE statement — two processes (API + sessions daemon) both
        calling ``start_run`` serialize on the status predicate and exactly one
        becomes the coordinator. Returns False when the run is already
        running/terminal (someone else owns it).
        """
        now = utc_now_iso()

        def _write(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE swarm_runs SET status = 'running', heartbeat_at = ?, "
                "started_at = COALESCE(started_at, ?), updated_at = ? "
                "WHERE id = ? AND status IN ('queued', 'planning')",
                (now, now, now, run_id),
            )
            return cursor.rowcount == 1

        return self._write_txn(_write, op="swarm_dal.try_activate_run")

    @_serialized
    def adopt_run(self, run_id: str, *, stale_minutes: float) -> bool:
        """Heartbeat-lease takeover CAS for ``resume_swarm`` (no double-coordinator).

        Adopts an ACTIVE (planning/running/merging) run ONLY when its coordinator
        heartbeat is older than ``stale_minutes`` (or NULL) — predicate and
        heartbeat write are one statement, so two resuming processes cannot
        both win. A fresh heartbeat means a live coordinator exists: returns
        False, caller must NOT coordinate.

        m10 fencing: adoption BUMPS ``lease_generation``, so a merely-wedged
        (not dead) original coordinator fails its next conditional heartbeat
        and stops instead of interleaving with the adopter.

        SCOPE LOCKS RIDE THE SAME BUMP. Every scope lock this run holds — the
        tasks' work locks and the coordinator's commit locks, all keyed by
        ``owner_group = run_id`` — is re-keyed to the NEW generation inside this
        same transaction. Without that, the bump would strand them: the displaced
        coordinator's cleanup path runs at the OLD generation, and
        ``release_scope``'s fence is ``holder_generation = ?``, so it would either
        free the locks the adopter is now working under (if the rows still
        carried the old generation) or the adopter could never renew them. Doing
        it here — one transaction, one CAS — means there is no instant in which a
        lock row's generation disagrees with its run's.

        The re-key is skipped outright when scope locking is off, which is not
        merely an optimization: with the mechanism off there are no rows to
        re-key, and the statement must not run at all for adoption to stay
        byte-for-byte what it is today.

        Inline SQL, NOT ``self.path_locks``: ``PathLockStore`` opens its own
        ``BEGIN IMMEDIATE``, and calling it from inside this transaction would
        re-enter the RLock and then fail with "cannot start a transaction within
        a transaction".
        """
        cutoff = (datetime.now(UTC) - timedelta(minutes=max(0.0, stale_minutes))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        now = utc_now_iso()
        rekey = scope_locks_enabled()
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE swarm_runs SET heartbeat_at = ?, updated_at = ?, "
                "lease_generation = lease_generation + 1 "
                f"WHERE id = ? AND status IN ({', '.join('?' for _ in _ACTIVE_STATUSES)}) "
                "AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
                (now, now, run_id, *_ACTIVE_STATUSES, cutoff),
            )
            adopted = cursor.rowcount == 1
            if adopted and rekey:
                # The subselect reads the generation this transaction just wrote,
                # so the locks land on the adopter's generation and never on a
                # value some other statement could have moved in between.
                self._connection.execute(
                    "UPDATE resource_locks SET holder_generation = "
                    "  (SELECT lease_generation FROM swarm_runs WHERE id = :run_id) "
                    "WHERE owner_group = :run_id AND lane = 'swarm' AND released_at IS NULL",
                    {"run_id": run_id},
                )
            self._commit()
            return adopted
        except BaseException:
            self._rollback()
            raise

    # --- provider-call ledger -------------------------------------------------

    @_serialized
    def record_provider_call(self, observation: CostObservation | dict[str, Any]) -> dict[str, Any]:
        """Persist one provider call, with idempotent same-payload replay."""

        self._begin()
        try:
            row = _record_provider_call_row(self._connection, observation)
            self._commit()
            return row
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def settle_provider_call(
        self,
        call_id: str,
        settlement: CostObservation | dict[str, Any],
    ) -> dict[str, Any]:
        """Settle one initiated call; conflicting second settlement fails closed."""

        self._begin()
        try:
            row = _settle_provider_call_row(self._connection, call_id, settlement)
            self._commit()
            return row
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def get_provider_call(self, call_id: str) -> dict[str, Any] | None:
        return _get_provider_call_row(self._connection, call_id)

    @_serialized
    def list_provider_calls(
        self,
        *,
        run_id: str | None = None,
        execution_id: str | None = None,
        campaign_id: str | None = None,
        reservation_id: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        where, parameters = _provider_call_where(
            {
                "run_id": run_id,
                "execution_id": execution_id,
                "campaign_id": campaign_id,
                "reservation_id": reservation_id,
            }
        )
        return _rows(
            self._connection.execute(
                f"SELECT * FROM provider_call_usage{where} ORDER BY created_at ASC, id ASC LIMIT ?",
                (*parameters, limit),
            ).fetchall()
        )

    @_serialized
    def aggregate_provider_call_cost(
        self,
        *,
        run_id: str | None = None,
        execution_id: str | None = None,
        campaign_id: str | None = None,
        reservation_id: str | None = None,
    ) -> dict[str, Any]:
        return _aggregate_provider_call_cost_rows(
            self._connection,
            run_id=run_id,
            execution_id=execution_id,
            campaign_id=campaign_id,
            reservation_id=reservation_id,
        )

    @_serialized
    def add_cost(self, run_id: str, delta_usd: float) -> float:
        """Accumulate one attempt's cost into ``swarm_runs.cost_usd``; returns
        the new total (the budget-gate input)."""

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE swarm_runs SET cost_usd = cost_usd + ?, updated_at = ? WHERE id = ?",
                (float(delta_usd), utc_now_iso(), run_id),
            )

        self._write_txn(_write, op="swarm_dal.add_cost")
        row = self._connection.execute(
            "SELECT cost_usd FROM swarm_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return float(row["cost_usd"]) if row is not None else 0.0

    @_serialized
    def budget_spend(self, run_id: str) -> RunBudgetSpend:
        """Read spend from provider-call truth with a legacy session floor.

        ``swarm_runs.cost_usd`` is only advanced when the scheduler settles a
        terminal attempt. Session usage can arrive before that, so the run
        column is a cache/floor rather than the authoritative pre-spawn value.
        The attempt value wins over its session backfill, matching
        :meth:`attempts_with_usage`.

        A tokens-only usage marker on a terminal session is unknown even if a
        legacy creation path left ``cost_usd=0.0`` behind. New writes use NULL,
        but interpreting the marker here keeps old rows from reopening the
        budget hole. A running session with no price yet is not permanently
        unpriced: its in-flight exposure is bounded by the scheduler's slot
        count and becomes known or explicitly unknown when it terminalizes.

        C4 adds a THIRD number beside those two, never mixed into either:
        ``estimated_cost_usd`` sums ``sessions.cost_estimate_usd`` (migration
        118) over exactly the sessions whose dollars are NOT known -- the same
        predicate as ``unknown_cost_sessions`` minus its terminal-state clause,
        because tokens already burned by a running session are already spent.
        A session with a known price contributes to ``known_cost_usd`` and is
        excluded here, so the two can never double-count the same work.
        """
        estimate_is_live = _ESTIMATE_IS_LIVE_SQL
        row = self._connection.execute(
            "SELECT r.cost_usd AS run_cost_usd, "
            "       COALESCE(SUM(CASE "
            "         WHEN a.usage_source = 'cli-report-tokens-only' THEN 0.0 "
            "         WHEN a.cost_usd IS NOT NULL THEN a.cost_usd "
            "         WHEN s.usage_source = 'cli-report-tokens-only' THEN 0.0 "
            "         ELSE COALESCE(s.cost_usd, 0.0) "
            "       END), 0.0) AS ledger_cost_usd, "
            "       COALESCE(SUM(CASE "
            "         WHEN s.id IS NOT NULL "
            "           AND s.state IN ('completed', 'failed', 'cancelled', 'killed') "
            "           AND ("
            "           a.usage_source = 'cli-report-tokens-only' "
            "           OR (a.cost_usd IS NULL AND ("
            "             s.usage_source = 'cli-report-tokens-only' "
            "             OR s.cost_usd IS NULL"
            "           ))"
            "         ) THEN 1 ELSE 0 "
            "       END), 0) AS unknown_cost_sessions, "
            f"       COALESCE(SUM(CASE WHEN {estimate_is_live} "  # noqa: S608 -- literal
            "         THEN s.cost_estimate_usd ELSE 0.0 END), 0.0) AS estimated_cost_usd, "
            f"       COALESCE(SUM(CASE WHEN {estimate_is_live} "  # noqa: S608 -- literal
            "         THEN 1 ELSE 0 END), 0) AS estimated_cost_sessions "
            "FROM swarm_runs r "
            "LEFT JOIN swarm_attempts a ON a.swarm_run_id = r.id "
            "LEFT JOIN sessions s ON s.id = a.session_id "
            "WHERE r.id = ? "
            "GROUP BY r.id",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown swarm run: {run_id}")
        # Some provider paths write the run aggregate without a durable
        # sessions-row join. Taking the greater observation prevents the live
        # join from regressing those already-accounted dollars.
        legacy_known = max(
            float(row["run_cost_usd"] or 0.0),
            float(row["ledger_cost_usd"] or 0.0),
        )
        legacy_unknown = int(row["unknown_cost_sessions"] or 0)
        session_estimated = float(row["estimated_cost_usd"] or 0.0)
        estimated_sessions = int(row["estimated_cost_sessions"] or 0)
        provider = _aggregate_provider_call_cost_rows(self._connection, run_id=run_id)
        provider_call_count = int(provider["call_count"])

        if provider_call_count == 0:
            incomplete = legacy_unknown > 0
            quality = (
                "mixed" if incomplete and legacy_known > 0 else "unknown" if incomplete else "exact"
            )
            return RunBudgetSpend(
                known_cost_usd=legacy_known,
                unknown_cost_sessions=legacy_unknown,
                known_usd_decimal=str(legacy_known),
                chargeable_usd=None if incomplete else legacy_known,
                quality=quality,
                safe_to_compare=not incomplete,
                accounting_incomplete=incomplete,
                estimated_cost_usd=session_estimated,
                estimated_cost_sessions=estimated_sessions,
            )

        provider_known = float(provider["known_usd"] or 0.0)
        known = max(legacy_known, provider_known)
        incomplete = bool(provider["accounting_incomplete"]) or legacy_unknown > 0
        quality = str(provider["quality"])
        if legacy_unknown and quality not in {"unknown", "mixed"}:
            quality = "mixed"
        known_decimal = provider["known_usd_decimal"]
        if legacy_known > provider_known:
            known_decimal = str(legacy_known)
        chargeable = None
        if not incomplete:
            provider_chargeable = float(provider["chargeable_usd"] or 0.0)
            chargeable = max(legacy_known, provider_chargeable)
        # Cap accrual counts BOTH estimate ledgers: session tokens priced by
        # migration 118 and provider calls the adapter could only bound. Both
        # are upper bounds over rows that contribute nothing to
        # ``known_cost_usd``, so this adds no measured dollar twice. Where a
        # single unit of work somehow lands in both ledgers the sum over-counts,
        # which is the safe direction for a ceiling -- and it cannot silently
        # halt a run, because ADVISORY never stops on a cap and BLOCK already
        # refuses on the unknown marker those same rows carry.
        provider_estimated = provider["estimated_upper_bound_usd"]
        return RunBudgetSpend(
            known_cost_usd=known,
            unknown_cost_sessions=legacy_unknown,
            known_usd_decimal=known_decimal,
            estimated_upper_bound_usd=provider_estimated,
            chargeable_usd=chargeable,
            unknown_call_count=int(provider["unknown_call_count"]),
            quality=quality,
            safe_to_compare=not incomplete,
            accounting_incomplete=incomplete,
            estimated_cost_usd=session_estimated + float(provider_estimated or 0.0),
            estimated_cost_sessions=estimated_sessions,
        )

    @_serialized
    def task_budget_reservation(self, board_task_id: str) -> float | None:
        """The UNSPENT dollars reserved for one card, or ``None`` if it holds no
        reservation at all.

        A split stamps each child with :data:`BUDGET_RESERVED_KEY` -- its slice
        of the entitlement its parent held. What the card may still spend is
        that slice MINUS what it has already burned: those dollars are already
        in :meth:`budget_spend`'s accrued total, so leaving them reserved as
        well would bill the same money twice and starve the card's siblings.
        Spend is counted with the same predicates as ``accrued_cost_usd``
        (priced attempt/session dollars plus migration-118 token estimates), so
        the two views of one card's cost cannot drift.

        ``None`` and ``0.0`` are deliberately different answers: ``None`` means
        "unreserved, entitled to the run's remaining headroom" (every root plan
        card, and every card in an uncapped run), while ``0.0`` means "reserved
        and exhausted". Collapsing them would hand an unreserved card a ceiling
        of zero and stall the run.
        """
        row = self._connection.execute(
            "SELECT t.swarm_json AS swarm_json, "
            f"       COALESCE(SUM({_KNOWN_ATTEMPT_COST_SQL}), 0.0) AS known_usd, "  # noqa: S608
            f"       COALESCE(SUM(CASE WHEN {_ESTIMATE_IS_LIVE_SQL} "  # noqa: S608 -- literal
            "         THEN s.cost_estimate_usd ELSE 0.0 END), 0.0) AS estimated_usd "
            "FROM board_tasks t "
            "LEFT JOIN swarm_attempts a ON a.board_task_id = t.id "
            "LEFT JOIN sessions s ON s.id = a.session_id "
            "WHERE t.id = ? "
            "GROUP BY t.id",
            (board_task_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            swarm_json = json.loads(str(row["swarm_json"] or "{}"))
        except (TypeError, ValueError):
            return None
        if not isinstance(swarm_json, dict):
            return None
        raw = swarm_json.get(BUDGET_RESERVED_KEY)
        if raw is None:
            return None
        try:
            reserved = float(raw)
        except (TypeError, ValueError):  # a corrupt stamp is not free money
            return 0.0
        spent = float(row["known_usd"] or 0.0) + float(row["estimated_usd"] or 0.0)
        return max(0.0, reserved - spent)

    @_serialized
    def project_budget_spend(self, project_id: str) -> ProjectBudgetSpend:
        """Return spend truth for all swarm runs assigned to ``project_id``.

        Summing individual run observations retains the attempt/session
        de-duplication rules of :meth:`budget_spend` and preserves unpriced
        session markers instead of turning them into free spend.
        """
        normalized = _normalized_project_id(project_id)
        if normalized is None:
            raise ValueError("project_id must not be blank")
        project = self._connection.execute(
            "SELECT budget_usd FROM projects WHERE id = ?", (normalized,)
        ).fetchone()
        rows = self._connection.execute(
            "SELECT id FROM swarm_runs WHERE project_id = ? ORDER BY id ASC", (normalized,)
        ).fetchall()
        spends = [self.budget_spend(str(row["id"])) for row in rows]
        return ProjectBudgetSpend(
            project_id=normalized,
            budget_usd=(
                None
                if project is None or project["budget_usd"] is None
                else float(project["budget_usd"])
            ),
            known_cost_usd=sum(spend.known_cost_usd for spend in spends),
            unknown_cost_sessions=sum(spend.unknown_cost_sessions for spend in spends),
            unknown_call_count=sum(spend.unknown_call_count for spend in spends),
            estimated_cost_usd=sum(spend.estimated_cost_usd for spend in spends),
            estimated_cost_sessions=sum(spend.estimated_cost_sessions for spend in spends),
        )

    @_serialized
    def active_projects(self) -> list[str]:
        """Project ids with a live swarm run, in deterministic order."""
        placeholders = ", ".join("?" for _ in _ACTIVE_STATUSES)
        rows = self._connection.execute(
            "SELECT DISTINCT project_id FROM swarm_runs "
            f"WHERE status IN ({placeholders}) AND project_id IS NOT NULL "
            "AND TRIM(project_id) != '' ORDER BY project_id ASC",
            _ACTIVE_STATUSES,
        ).fetchall()
        return [str(row["project_id"]) for row in rows]

    @_serialized
    def fair_share_allocation(self) -> dict[str, float]:
        """Divide remaining global dollars evenly among eligible active projects.

        The global limit is the existing ``budgets.id='global'`` ledger.  If
        any active project's total is unpriced, no fair allocation is returned:
        subtracting an unknown amount would create invented headroom.
        """
        global_budget = self._connection.execute(
            "SELECT cost_usd_max, used_cost_usd FROM budgets WHERE id = 'global'"
        ).fetchone()
        if global_budget is None or global_budget["cost_usd_max"] is None:
            return {}
        active = self.active_projects()
        if not active:
            return {}
        spends = [self.project_budget_spend(project_id) for project_id in active]
        if any(not spend.safe_to_compare for spend in spends):
            return {}
        eligible = [
            spend.project_id
            for spend in spends
            # Accrued, not known: an in-flight session's tokens are already
            # burned, so handing out headroom against them would invent it.
            if spend.budget_usd is None or spend.accrued_cost_usd < spend.budget_usd
        ]
        if not eligible:
            return {}
        remaining = max(
            0.0,
            float(global_budget["cost_usd_max"]) - float(global_budget["used_cost_usd"] or 0.0),
        )
        share = remaining / len(eligible)
        return {project_id: share for project_id in eligible}

    @_serialized
    def set_plan(self, run_id: str, plan: dict[str, Any]) -> bool:
        """Replace ``plan_json`` (split/resize bump the derived plan projection)."""

        def _write(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE swarm_runs SET plan_json = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(plan, separators=(",", ":"), sort_keys=True),
                    utc_now_iso(),
                    run_id,
                ),
            )
            return cursor.rowcount == 1

        return self._write_txn(_write, op="swarm_dal.set_plan")

    @_serialized
    def get_run_params(self, run_id: str) -> dict[str, Any]:
        """The run's durable execution params (``params_json``, m6) — e.g.
        the worktree_mode RESOLVED at activation. ``{}`` when unset/invalid."""
        row = self._connection.execute(
            "SELECT params_json FROM swarm_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return {}
        try:
            parsed = json.loads(row["params_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @_serialized
    def merge_run_params(self, run_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Merge ``updates`` into ``params_json`` (single serialized RMW)."""
        row = self._connection.execute(
            "SELECT params_json FROM swarm_runs WHERE id = ?", (run_id,)
        ).fetchone()
        try:
            current = json.loads((row["params_json"] if row else None) or "{}")
        except (json.JSONDecodeError, TypeError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        current.update(updates)

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE swarm_runs SET params_json = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(current, separators=(",", ":"), sort_keys=True),
                    utc_now_iso(),
                    run_id,
                ),
            )

        self._write_txn(_write, op="swarm_dal.merge_run_params")
        return current

    @_serialized
    def set_metrics(self, run_id: str, metrics: dict[str, Any]) -> bool:
        metrics_json = json.dumps(metrics, separators=(",", ":"), sort_keys=True)

        def _write(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE swarm_runs SET metrics_json = ?, updated_at = ? WHERE id = ?",
                (metrics_json, utc_now_iso(), run_id),
            )
            return cursor.rowcount == 1

        return self._write_txn(_write, op="swarm_dal.set_metrics")

    @_serialized
    def set_summary_note_path(self, run_id: str, path: str | None) -> bool:
        """Persist the WP7 vault note path onto ``swarm_runs.summary_note_path``."""

        def _write(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE swarm_runs SET summary_note_path = ?, updated_at = ? WHERE id = ?",
                (path, utc_now_iso(), run_id),
            )
            return cursor.rowcount == 1

        return self._write_txn(_write, op="swarm_dal.set_summary_note_path")

    # --- fleet admission ------------------------------------------------------

    @_serialized
    def active_run_count(self) -> int:
        """Runs currently holding fleet capacity (planning/running/merging)."""
        row = self._connection.execute(
            "SELECT COUNT(*) AS n FROM swarm_runs WHERE status IN (?, ?, ?)",
            _ACTIVE_STATUSES,
        ).fetchone()
        return int(row["n"])

    @_serialized
    def live_attempt_count(self) -> int:
        """Fleet-wide count of ``swarm_attempts`` with no ``ended_at`` (WP6a utilization stub).

        A real cross-lane fleet ledger is WP2's ``limit_state.py`` job (see the
        plan doc's "Fleet capacity model"); this is a swarm-only stopgap so
        ``GET /api/swarm``'s fleet list has SOME utilization signal before
        that lands.
        """
        row = self._connection.execute(
            "SELECT COUNT(*) AS n FROM swarm_attempts WHERE ended_at IS NULL"
        ).fetchone()
        return int(row["n"])

    @_serialized
    def oldest_queued(self) -> dict[str, Any] | None:
        """Admission control starts queued runs oldest-first as capacity frees.

        created_at is second-granularity, so rowid (insertion order) breaks ties
        — random-uuid ids would make same-second admission order arbitrary.
        """
        row = self._connection.execute(
            "SELECT * FROM swarm_runs WHERE status = 'queued' "
            "ORDER BY created_at ASC, rowid ASC LIMIT 1"
        ).fetchone()
        return None if row is None else dict(row)

    # --- board membership + DAG -----------------------------------------------

    @_serialized
    def assign_task_to_run(
        self,
        board_task_id: str,
        swarm_run_id: str,
        swarm_json: dict[str, Any] | None = None,
    ) -> bool:
        """Bind a board card to a swarm run (membership = swarm_run_id ONLY).

        ``lane`` and ``category_id`` are never touched: lane is the frozen
        execution-ownership axis, category is cross-lane metadata a card may
        already carry. Raises ValueError on an unknown card, and on a
        ``lane='longhaul'`` card — the longhaul engine owns that card's
        execution, so it must never also be bound to a swarm run.
        """
        self._begin()
        try:
            row = self._connection.execute(
                "SELECT id, lane FROM board_tasks WHERE id = ?", (board_task_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown board task: {board_task_id}")
            if row["lane"] == "longhaul":
                raise ValueError(f"cannot bind longhaul-lane card to a swarm run: {board_task_id}")
            assignments = ["swarm_run_id = ?", "updated_at = ?"]
            values: list[Any] = [swarm_run_id, utc_now_iso()]
            if swarm_json is not None:
                assignments.append("swarm_json = ?")
                values.append(json.dumps(swarm_json, separators=(",", ":"), sort_keys=True))
            values.append(board_task_id)
            cursor = self._connection.execute(
                f"UPDATE board_tasks SET {', '.join(assignments)} WHERE id = ?", values
            )
            self._commit()
            return cursor.rowcount == 1
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def set_swarm_json(self, board_task_id: str, swarm_json: dict[str, Any]) -> bool:
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE board_tasks SET swarm_json = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(swarm_json, separators=(",", ":"), sort_keys=True),
                    utc_now_iso(),
                    board_task_id,
                ),
            )
            self._commit()
            return cursor.rowcount == 1
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def get_swarm_json(self, board_task_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT swarm_json FROM board_tasks WHERE id = ?", (board_task_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            parsed = json.loads(row["swarm_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @_serialized
    def add_deps(self, swarm_run_id: str, edges: list[tuple[str, str]]) -> None:
        """Insert DAG edges ``(task_id, depends_on_task_id)`` in one transaction."""
        if not edges:
            return
        self._begin()
        try:
            for task_id, depends_on_task_id in edges:
                self._connection.execute(
                    "INSERT OR IGNORE INTO swarm_deps "
                    "(swarm_run_id, task_id, depends_on_task_id) VALUES (?, ?, ?)",
                    (swarm_run_id, task_id, depends_on_task_id),
                )
            self._commit()
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def tasks_for_run(self, swarm_run_id: str) -> list[dict[str, Any]]:
        """All ``board_tasks`` bound to a run, any status — progress/detail reporting.

        Unlike :meth:`eligible_tasks` (open + unclaimed + ready-to-run only),
        this returns every member card regardless of lifecycle state, which is
        what ``GET /api/swarm``'s fleet progress counts and
        ``GET /api/swarm/{id}``'s task list both need.
        """
        return _rows(
            self._connection.execute(
                "SELECT * FROM board_tasks WHERE swarm_run_id = ? ORDER BY created_at ASC, id ASC",
                (swarm_run_id,),
            ).fetchall()
        )

    @_serialized
    def deps_for_run(self, swarm_run_id: str) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM swarm_deps WHERE swarm_run_id = ? "
                "ORDER BY task_id ASC, depends_on_task_id ASC",
                (swarm_run_id,),
            ).fetchall()
        )

    @_serialized
    def eligible_tasks(self, swarm_run_id: str) -> list[dict[str, Any]]:
        """Open, unclaimed run members whose prerequisites are ALL done.

        One query: open + unarchived + unclaimed, zero non-done deps
        (NOT EXISTS join), and no live attempt row (defense-in-depth — an open
        task should never have one, but a crashed claim-release must not make
        a task double-runnable).
        """
        return _rows(
            self._connection.execute(
                "SELECT t.* FROM board_tasks t "
                "WHERE t.swarm_run_id = ? "
                "  AND t.status = 'open' "
                "  AND t.archived_at IS NULL "
                "  AND t.claimed_by IS NULL "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM swarm_deps d "
                "    JOIN board_tasks p ON p.id = d.depends_on_task_id "
                "    WHERE d.task_id = t.id AND p.status <> 'done'"
                "  ) "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM swarm_attempts a "
                "    WHERE a.board_task_id = t.id AND a.ended_at IS NULL"
                "  ) "
                "ORDER BY t.created_at ASC, t.id ASC",
                (swarm_run_id,),
            ).fetchall()
        )

    @_serialized
    def propagate_blocked(self, swarm_run_id: str, blocked_task_id: str) -> list[str]:
        """Block every transitive dependent of a blocked task (one transaction).

        The integration task is exempt (``swarm_json.integration`` truthy): it
        runs over whatever completed and marks the summary partial. Tasks
        already terminal (done/cancelled/blocked) are left untouched but still
        traversed, so grandchildren behind an already-blocked child are caught.
        Returns the ids actually flipped to blocked.
        """
        self._begin()
        try:
            edges = self._connection.execute(
                "SELECT task_id, depends_on_task_id FROM swarm_deps WHERE swarm_run_id = ?",
                (swarm_run_id,),
            ).fetchall()
            dependents: dict[str, list[str]] = {}
            for edge in edges:
                dependents.setdefault(str(edge["depends_on_task_id"]), []).append(
                    str(edge["task_id"])
                )
            seen: set[str] = set()
            frontier = list(dependents.get(blocked_task_id, []))
            while frontier:
                task_id = frontier.pop()
                if task_id in seen:
                    continue
                seen.add(task_id)
                frontier.extend(dependents.get(task_id, []))

            updated: list[str] = []
            now = utc_now_iso()
            for task_id in sorted(seen):
                row = self._connection.execute(
                    "SELECT status, swarm_json FROM board_tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is None or str(row["status"]) in _TERMINAL_TASK_STATUSES:
                    continue
                try:
                    swarm_json = json.loads(row["swarm_json"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    swarm_json = {}
                if isinstance(swarm_json, dict) and swarm_json.get("integration"):
                    continue
                self._connection.execute(
                    "UPDATE board_tasks SET status = 'blocked', updated_at = ? WHERE id = ?",
                    (now, task_id),
                )
                updated.append(task_id)
            self._commit()
            return updated
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def split_task(
        self,
        swarm_run_id: str,
        parent_task_id: str,
        subtask_cards: list[dict[str, Any]],
        *,
        plan_json: str | None = None,
    ) -> dict[str, Any]:
        """Split one task into ≤4 subtasks, rewiring the DAG ATOMICALLY.

        One BEGIN IMMEDIATE transaction (the WP5a bullet: "split rewires
        dependencies atomically in one transaction"):

        1. subtasks are inserted as new open board cards bound to the run, in
           the PARENT's project (M1 — a split re-shapes work, it never moves it
           between projects, and an unscoped parent yields unscoped subtasks);
        2. every subtask INHERITS the parent's prerequisites (edges
           ``subtask -> parent_dep``);
        3. every dependent of the parent is RE-POINTED to ALL subtasks (its
           edge on the parent is deleted, edges on each subtask inserted) —
           a dependent can never observe a state where it depends on neither;
        4. the parent CLOSES as split: status ``cancelled`` (terminal, so run
           completion accounting works), claim cleared, ``swarm_json`` merged
           with ``{"split": true, "subtask_ids": [...]}`` so summaries and the
           partial flag can tell a split parent from a genuinely dead task.

        ``plan_json`` (already serialized, as :meth:`set_plan` would store it)
        replaces the run's derived plan projection in the SAME transaction. A
        split bumps ``plan.version``, and the child cards are stamped with that
        post-bump lineage; writing the bump separately left a window in which
        the children and the plan disagreed about which revision minted them.
        ``None`` leaves ``plan_json`` untouched — the caller could not rebuild
        the projection, so no bump happened and the parent's lineage still
        stands. Serialization is the CALLER's job on purpose: projection upkeep
        must never be able to fail the split from inside its transaction.

        Card dicts use the ``provision_run`` shape (id, title, description,
        swarm_json, priority). Raises ValueError on 0 or >4 subtasks, an
        unknown/terminal parent, or a parent outside the run — and any failure
        rolls the WHOLE rewiring back.
        Returns ``{"parent_id", "subtask_ids", "rewired_dependents"}``.
        """
        if not subtask_cards or len(subtask_cards) > 4:
            raise ValueError(f"task split must produce 1-4 subtasks, got {len(subtask_cards)}")
        now = utc_now_iso()
        self._begin()
        try:
            parent = self._connection.execute(
                "SELECT status, swarm_run_id, swarm_json, project_id FROM board_tasks WHERE id = ?",
                (parent_task_id,),
            ).fetchone()
            if parent is None or str(parent["swarm_run_id"] or "") != swarm_run_id:
                raise ValueError(f"task {parent_task_id} is not a member of run {swarm_run_id}")
            if str(parent["status"]) in _TERMINAL_TASK_STATUSES:
                raise ValueError(f"cannot split terminal task {parent_task_id}")
            # M1: a split is a re-shape of the SAME work, so the subtasks are in
            # the same project as the card they replace. Taking it from the
            # parent row (not the run) keeps the two consistent even for a card
            # whose project was corrected after provisioning.
            parent_project_id = _normalized_project_id(parent["project_id"])

            parent_deps = [
                str(row["depends_on_task_id"])
                for row in self._connection.execute(
                    "SELECT depends_on_task_id FROM swarm_deps "
                    "WHERE swarm_run_id = ? AND task_id = ?",
                    (swarm_run_id, parent_task_id),
                ).fetchall()
            ]
            dependents = [
                str(row["task_id"])
                for row in self._connection.execute(
                    "SELECT task_id FROM swarm_deps "
                    "WHERE swarm_run_id = ? AND depends_on_task_id = ?",
                    (swarm_run_id, parent_task_id),
                ).fetchall()
            ]

            subtask_ids: list[str] = []
            for card in subtask_cards:
                card_id = str(card.get("id") or new_id("btk"))
                subtask_ids.append(card_id)
                self._connection.execute(
                    "INSERT INTO board_tasks "
                    "(id, title, description, required_expertise_json, discipline, "
                    "priority, status, claimed_by, claim_version, result_ref, "
                    "created_at, updated_at, swarm_run_id, swarm_json, project_id) "
                    "VALUES (?, ?, ?, '[]', ?, ?, 'open', NULL, 0, NULL, ?, ?, ?, ?, ?)",
                    (
                        card_id,
                        str(card.get("title", "")),
                        str(card.get("description", "")),
                        card.get("discipline"),
                        str(card.get("priority", "normal")),
                        now,
                        now,
                        swarm_run_id,
                        json.dumps(
                            card.get("swarm_json") or {},
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        parent_project_id,
                    ),
                )
                for dep in parent_deps:  # subtasks inherit the parent's deps
                    self._connection.execute(
                        "INSERT OR IGNORE INTO swarm_deps "
                        "(swarm_run_id, task_id, depends_on_task_id) VALUES (?, ?, ?)",
                        (swarm_run_id, card_id, dep),
                    )

            # Re-point every dependent at ALL subtasks, then drop its parent edge.
            for dependent in dependents:
                for subtask_id in subtask_ids:
                    self._connection.execute(
                        "INSERT OR IGNORE INTO swarm_deps "
                        "(swarm_run_id, task_id, depends_on_task_id) VALUES (?, ?, ?)",
                        (swarm_run_id, dependent, subtask_id),
                    )
            self._connection.execute(
                "DELETE FROM swarm_deps WHERE swarm_run_id = ? AND depends_on_task_id = ?",
                (swarm_run_id, parent_task_id),
            )
            self._connection.execute(
                "DELETE FROM swarm_deps WHERE swarm_run_id = ? AND task_id = ?",
                (swarm_run_id, parent_task_id),
            )

            try:
                parent_swarm_json = json.loads(parent["swarm_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                parent_swarm_json = {}
            if not isinstance(parent_swarm_json, dict):
                parent_swarm_json = {}
            parent_swarm_json["split"] = True
            parent_swarm_json["subtask_ids"] = subtask_ids
            self._connection.execute(
                "UPDATE board_tasks SET status = 'cancelled', claimed_by = NULL, "
                "claim_version = claim_version + 1, swarm_json = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    json.dumps(parent_swarm_json, separators=(",", ":"), sort_keys=True),
                    now,
                    parent_task_id,
                ),
            )
            if plan_json is not None:
                self._connection.execute(
                    "UPDATE swarm_runs SET plan_json = ?, updated_at = ? WHERE id = ?",
                    (plan_json, now, swarm_run_id),
                )
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return {
            "parent_id": parent_task_id,
            "subtask_ids": subtask_ids,
            "rewired_dependents": dependents,
        }

    # --- transactional provisioning (WP4) ---------------------------------------

    @_serialized
    def provision_run(
        self,
        *,
        run: dict[str, Any],
        root_card: dict[str, Any],
        cards: list[dict[str, Any]],
        edges: list[tuple[str, str]],
    ) -> dict[str, Any]:
        """Write the run row + root card + child cards + DAG edges in ONE transaction.

        The WP4 invariant: a swarm plan is live instantly or not at all — a crash
        mid-provision must leave no half-provisioned run (no orphan cards, no
        run row without its cards). Any failure rolls back everything.

        ``run`` fields: id (optional), board_task_id, project_id, working_dir,
        goal, status (default 'planning'), plan (dict → plan_json),
        target_concurrency, max_concurrency, budget_usd_max.

        M1 project axis: ``run["project_id"]`` is stamped on the run row AND on
        every card provisioned with it (``board_tasks.project_id``, migration
        087), inside the one transaction — so a run and its board are never in
        different projects. It is not defaulted: absent/blank means NULL,
        "unscoped", which is exactly what a NULL grant binding means to the
        broker (migration 115). Nothing here invents a project for a caller
        that did not name one.

        Card dicts: id, title, description, status, swarm_json (dict),
        priority/discipline/category_id optional — ``category_id`` is
        cross-lane board-taxonomy metadata (longhaul WIP counting is
        lane-scoped, so it is inert to the longhaul engine). ``edges`` are
        (task_card_id, depends_on_card_id) pairs. Returns the inserted run row.
        """
        status = str(run.get("status", "planning"))
        if status not in {str(value) for value in SwarmRunStatus}:
            raise ValueError(f"invalid swarm run status: {status!r}")
        run_id = str(run.get("id") or new_id("swr"))
        now = utc_now_iso()
        plan_json = json.dumps(run.get("plan") or {}, separators=(",", ":"), sort_keys=True)
        # M1: the run's project is stamped onto every card it provisions, in the
        # SAME transaction. A card is work IN a project or it is unscoped —
        # there is no third state where the run knows its project and its own
        # cards do not, which is what shipped before this line (the column from
        # 087 existed, nothing on this path ever wrote it). NULL stays NULL:
        # unscoped runs produce unscoped cards, never a guessed default.
        run_project_id = _normalized_project_id(run.get("project_id"))
        self._begin()
        try:
            self._connection.execute(
                "INSERT INTO swarm_runs "
                "(id, board_task_id, project_id, working_dir, goal, status, plan_json, "
                "target_concurrency, max_concurrency, budget_usd_max, heartbeat_at, "
                "started_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    run.get("board_task_id"),
                    run_project_id,
                    str(run.get("working_dir", "")),
                    str(run.get("goal", "")),
                    status,
                    plan_json,
                    int(run.get("target_concurrency", 1)),
                    int(run.get("max_concurrency", 10)),
                    run.get("budget_usd_max"),
                    now,
                    now if status != "queued" else None,
                    now,
                    now,
                ),
            )
            for card in (root_card, *cards):
                self._connection.execute(
                    "INSERT INTO board_tasks "
                    "(id, title, description, required_expertise_json, discipline, "
                    "priority, status, claimed_by, claim_version, result_ref, "
                    "created_at, updated_at, swarm_run_id, swarm_json, category_id, "
                    "project_id) "
                    "VALUES (?, ?, ?, '[]', ?, ?, ?, NULL, 0, NULL, ?, ?, ?, ?, ?, ?)",
                    (
                        str(card["id"]),
                        str(card.get("title", "")),
                        str(card.get("description", "")),
                        card.get("discipline"),
                        str(card.get("priority", "normal")),
                        str(card.get("status", "open")),
                        now,
                        now,
                        run_id,
                        json.dumps(
                            card.get("swarm_json") or {},
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        card.get("category_id"),
                        run_project_id,
                    ),
                )
            for task_card_id, depends_on_card_id in edges:
                self._connection.execute(
                    "INSERT INTO swarm_deps "
                    "(swarm_run_id, task_id, depends_on_task_id) VALUES (?, ?, ?)",
                    (run_id, task_card_id, depends_on_card_id),
                )
            self._commit()
        except BaseException:
            self._rollback()
            raise
        row = self._connection.execute(
            "SELECT * FROM swarm_runs WHERE id = ?", (run_id,)
        ).fetchone()
        return dict(row)

    # --- attempts -------------------------------------------------------------

    @_serialized
    def open_attempt(
        self,
        swarm_run_id: str,
        board_task_id: str,
        *,
        provider: str,
        model: str,
        tier: str | None = None,
        account_id: str | None = None,
        session_id: str | None = None,
        effort: str | None = None,
        source: str = "real",
        verdict_hash: str | None = None,
        tool_names: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Open a new executor attempt (043 idiom: guards inside the transaction).

        ``effort`` is the router-decided reasoning effort for this attempt
        (048; NULL for pre-048 rows or providers running at session default).

        ``verdict_hash`` (109) is the loop-progress fingerprint written by the
        four dispatch pumps: sha256 of the reviewer verdict BODY this attempt is
        reacting to. The scheduler's executor leaves it NULL — "not pump-driven"
        — and ``retry_cap_exceeded`` counts only non-NULL rows, so executor
        retry accounting is unchanged by this column's existence.

        ``tool_names`` (129) is optional because legacy callers do not carry an
        exposed tool set. When supplied, its canonical digest is stored on the
        attempt row in the same transaction; when absent, the column stays NULL.

        Raises ValueError for terminal/archived/foreign tasks and RuntimeError
        when a live attempt already exists.
        """
        self._begin()
        try:
            board_row = self._connection.execute(
                "SELECT status, archived_at, swarm_run_id FROM board_tasks WHERE id = ?",
                (board_task_id,),
            ).fetchone()
            if (
                board_row is None
                or str(board_row["status"]) in _TERMINAL_TASK_STATUSES
                or board_row["archived_at"] is not None
            ):
                raise ValueError(f"cannot open attempt for terminal/archived task {board_task_id}")
            if str(board_row["swarm_run_id"] or "") != swarm_run_id:
                raise ValueError(
                    f"board task {board_task_id} is not a member of run {swarm_run_id}"
                )
            live = self._connection.execute(
                "SELECT id FROM swarm_attempts WHERE board_task_id = ? AND ended_at IS NULL",
                (board_task_id,),
            ).fetchone()
            if live is not None:
                raise RuntimeError(
                    f"cannot open a new attempt: task {board_task_id} already has a live attempt"
                )
            seq_row = self._connection.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq FROM swarm_attempts "
                "WHERE board_task_id = ?",
                (board_task_id,),
            ).fetchone()
            seq = int(seq_row["next_seq"])
            attempt_id = new_id("swa")
            now = utc_now_iso()
            self._connection.execute(
                "INSERT INTO swarm_attempts "
                "(id, swarm_run_id, board_task_id, seq, session_id, provider, model, "
                "tier, account_id, effort, source, verdict_hash, started_at, "
                "ended_at, end_reason, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, '')",
                (
                    attempt_id,
                    swarm_run_id,
                    board_task_id,
                    seq,
                    session_id,
                    provider,
                    model,
                    tier,
                    account_id,
                    effort,
                    source,
                    verdict_hash,
                    now,
                ),
            )
            tool_set_digest = None
            if tool_names is not None:
                tool_set_digest = record_tool_set_digest(
                    self._connection,
                    attempt_id=attempt_id,
                    tool_names=tool_names,
                )
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return {
            "id": attempt_id,
            "swarm_run_id": swarm_run_id,
            "board_task_id": board_task_id,
            "seq": seq,
            "session_id": session_id,
            "provider": provider,
            "model": model,
            "tier": tier,
            "account_id": account_id,
            "effort": effort,
            "source": source,
            "verdict_hash": verdict_hash,
            "tool_set_digest": tool_set_digest,
            "started_at": now,
            "ended_at": None,
            "end_reason": None,
            "detail": "",
        }

    @_serialized
    def bind_attempt_session(self, attempt_id: str, session_id: str) -> bool:
        """Persist the spawned session onto a live attempt row.

        The scheduler opens the attempt BEFORE spawning (the cancel re-check
        sits between); binding the session id afterwards is what lets
        ``resume_swarm`` re-attach a crashed coordinator's live attempts to
        their still-running sessions instead of respawning.
        """
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE swarm_attempts SET session_id = ? WHERE id = ? AND ended_at IS NULL",
                (session_id, attempt_id),
            )
            self._commit()
            return cursor.rowcount == 1
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def attempts_with_usage(self, swarm_run_id: str) -> list[dict[str, Any]]:
        """Attempt rows for a run with usage filled in from their session.

        Usage lands in two places by design. The PROCESS knows what it spent
        (tokens, cost, wall-clock) and writes it to the session it owns; the
        SCHEDULER knows what it chose (effort, tier) and writes that to the
        attempt. Neither can see the other's half, so cost-to-green joins them
        here rather than forcing one side to learn about the other.

        The attempt's own value wins where present -- a caller that recorded
        directly onto the attempt meant it -- and the session backfills the
        rest. Attempts with no session row (a provider-exec path that never
        created one) simply keep their NULLs, which ``costgreen`` reports as
        unmeasured rather than as zero.
        """
        rows = self._connection.execute(
            "SELECT a.id, a.swarm_run_id, a.board_task_id, a.seq, a.session_id, "
            "       a.provider, a.model, a.tier, a.account_id, a.started_at, "
            "       a.ended_at, a.end_reason, a.detail, "
            "       COALESCE(a.effort, s.effort) AS effort, "
            "       COALESCE(a.cost_usd, s.cost_usd) AS cost_usd, "
            "       COALESCE(a.input_tokens, s.input_tokens) AS input_tokens, "
            "       COALESCE(a.output_tokens, s.output_tokens) AS output_tokens, "
            "       COALESCE(a.wall_ms, s.wall_ms) AS wall_ms, "
            "       COALESCE(a.usage_source, s.usage_source) AS usage_source "
            "FROM swarm_attempts a LEFT JOIN sessions s ON s.id = a.session_id "
            "WHERE a.swarm_run_id = ? ORDER BY a.board_task_id ASC, a.seq ASC",
            (swarm_run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @_serialized
    def record_attempt_usage(
        self,
        attempt_id: str,
        *,
        effort: str | None = None,
        cost_usd: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        wall_ms: int | None = None,
        usage_source: str | None = None,
    ) -> bool:
        """Attach measured usage to an attempt (migration 049 telemetry).

        Every field is optional and only non-None values are written, so a
        provider that reports tokens but not cost leaves cost NULL rather than
        zeroing it -- ``costgreen`` distinguishes "spent nothing" from "did not
        say", and conflating them would silently bias the effort comparison
        toward whichever provider reports least.

        Unlike ``close_attempt`` this does NOT require the attempt to be live:
        usage is known at process exit, which may land either side of the close.
        """
        updates: dict[str, Any] = {
            "effort": effort,
            "cost_usd": cost_usd,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "wall_ms": wall_ms,
            "usage_source": usage_source,
        }
        assignments = {key: value for key, value in updates.items() if value is not None}
        if not assignments:
            return False
        clause = ", ".join(f"{key} = ?" for key in assignments)
        self._begin()
        try:
            cursor = self._connection.execute(
                f"UPDATE swarm_attempts SET {clause} WHERE id = ?",
                (*assignments.values(), attempt_id),
            )
            self._commit()
            return cursor.rowcount == 1
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def register_attempt_close_hook(
        self, hook: Callable[[dict[str, Any]], None]
    ) -> None:
        """Register a callable to be invoked with the terminal state after an attempt closes.

        The hook is called AFTER the CAS succeeds and outside any database transaction.
        Hook exceptions are logged and do not affect close_attempt's return value.
        """
        self._attempt_close_hooks.append(hook)

    def unregister_attempt_close_hook(
        self, hook: Callable[[dict[str, Any]], None]
    ) -> None:
        """Unregister a previously registered attempt close hook."""
        try:
            self._attempt_close_hooks.remove(hook)
        except ValueError:
            pass  # Hook was not registered; silently ignore

    def _invoke_attempt_close_hooks(self, terminal_state: dict[str, Any]) -> None:
        """Invoke all registered attempt close hooks with the terminal state.

        Exceptions in hooks are logged but do not propagate.
        """
        for hook in self._attempt_close_hooks:
            try:
                hook(terminal_state)
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).debug(
                    "attempt_close_hook raised; continuing",
                    exc_info=True,
                )

    @_serialized
    def close_attempt(self, attempt_id: str, end_reason: str, detail: str = "") -> bool:
        """Idempotent CAS close on ``ended_at IS NULL`` (043's close idiom).

        Returns True if this call closed the attempt, False if already closed.
        """
        if end_reason not in ATTEMPT_END_REASONS:
            raise ValueError(f"invalid end_reason: {end_reason!r}")
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE swarm_attempts SET ended_at = ?, end_reason = ?, detail = ? "
                "WHERE id = ? AND ended_at IS NULL",
                (utc_now_iso(), end_reason, detail, attempt_id),
            )
            self._commit()
            closed = cursor.rowcount == 1
        except BaseException:
            self._rollback()
            raise
        if closed:
            # PKG-INSESSION-FANOUT: a closed attempt can never spawn again —
            # release its grant's committed agent capacity on EVERY close path
            # (belt; the hook's attempt-liveness join and the TTL are the
            # braces, so a failure here is hygiene, not safety).
            try:
                from omniagentos.swarm.insession import void_grant_for_attempt

                void_grant_for_attempt(attempt_id, f"attempt_closed:{end_reason}")
            except Exception:  # noqa: BLE001 -- accounting hygiene must not fail a close.
                logging.getLogger(__name__).debug(
                    "insession grant void failed for %s", attempt_id, exc_info=True
                )
        # Invoke attempt close hooks with terminal state after CAS success
        if closed:
            try:
                terminal_state = self._connection.execute(
                    "SELECT * FROM swarm_attempts WHERE id = ?",
                    (attempt_id,),
                ).fetchone()
                if terminal_state is not None:
                    self._invoke_attempt_close_hooks(dict(terminal_state))
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).debug(
                    "attempt_close_hooks invocation failed for %s",
                    attempt_id,
                    exc_info=True,
                )

        return closed

    @_serialized
    def current_attempt(self, board_task_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM swarm_attempts WHERE board_task_id = ? AND ended_at IS NULL",
            (board_task_id,),
        ).fetchone()
        return None if row is None else dict(row)

    @_serialized
    def attempt_for_session(self, session_id: str) -> dict[str, Any] | None:
        """Newest attempt bound to ``session_id``, or None.

        The transcript-synthesis path (``GET /api/sessions/{id}/transcript``)
        maps a provider session back to its swarm attempt so the response can
        carry attempt metadata and the task workbook. A session id is bound to
        at most one live attempt (``bind_attempt_session``); newest-first keeps
        a hypothetical rebind deterministic.
        """
        if not session_id:
            return None
        row = self._connection.execute(
            "SELECT * FROM swarm_attempts WHERE session_id = ? "
            "ORDER BY started_at DESC, seq DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return None if row is None else dict(row)

    @_serialized
    def list_attempts(self, board_task_id: str) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM swarm_attempts WHERE board_task_id = ? ORDER BY seq ASC",
                (board_task_id,),
            ).fetchall()
        )

    @_serialized
    def attempts_for_run(self, swarm_run_id: str) -> list[dict[str, Any]]:
        """Every ``swarm_attempts`` row for a run, oldest-first — WP7's raw
        input for per-task durations, first-attempt rate, and retry counts."""
        return _rows(
            self._connection.execute(
                "SELECT * FROM swarm_attempts WHERE swarm_run_id = ? "
                "ORDER BY started_at ASC, seq ASC",
                (swarm_run_id,),
            ).fetchall()
        )

    # --- pump ledger (109) -----------------------------------------------------
    #
    # The four dispatch pumps (rework / review / verdict / sim) are shell loops,
    # not scheduler workers, so they have no run and no board card of their own.
    # Before 109 that meant they wrote NOTHING: rework-pump and sim-pump did not
    # touch the ledger at all, review-pump and verdict-pump only READ it
    # (`fleet-ledger.py scan/query`). With no row written, the scheduler's
    # 2-retry cap governed none of them, and rework-pump re-dispatched one
    # brief-less lane 726 times against an identical verdict.
    #
    # These methods give a pump lane the minimum real identity the EXISTING
    # attempt API requires — one board card per lane, bound to a synthetic run
    # id — so `open_attempt`/`record_attempt_usage`/`close_attempt` work
    # unchanged. There is deliberately NO `swarm_runs` row: nothing joins
    # attempts to runs on this path, and inserting a fake "completed" run would
    # permanently inflate every completed-run count in the reporting surface.
    # The synthetic id is a namespace, not a run.

    @_serialized
    def ensure_pump_lane(self, lane: str, *, working_dir: str = "") -> str:
        """Idempotently anchor one pump lane to a board card; return its id.

        Running twice equals running once: the id is a pure function of the
        lane name, and the insert is OR IGNORE. The card is never archived and
        never terminal, because ``open_attempt`` refuses both — a "tidier"
        hidden card would silently disarm the whole gate.
        """
        lane = str(lane).strip()
        if not lane:
            raise ValueError("pump lane name must be non-empty")
        task_id = pump_lane_task_id(lane)
        now = utc_now_iso()
        self._begin()
        try:
            self._connection.execute(
                "INSERT OR IGNORE INTO board_tasks "
                "(id, title, description, required_expertise_json, discipline, "
                "priority, status, claimed_by, claim_version, result_ref, "
                "created_at, updated_at, swarm_run_id, swarm_json, category_id) "
                "VALUES (?, ?, ?, '[]', NULL, 'low', 'open', NULL, 0, NULL, "
                "?, ?, ?, '{}', NULL)",
                (
                    task_id,
                    f"[pump-ledger] {lane}",
                    "Ledger anchor for a dispatch-pump lane (migration 109). "
                    "Not a work item: it exists so every pump dispatch can write "
                    "a swarm_attempts row the retry cap can read.",
                    now,
                    now,
                    PUMP_LEDGER_RUN_ID,
                ),
            )
            # A card that predates this call may have drifted terminal/archived
            # (e.g. a board sweep). Restore exactly the two properties
            # open_attempt requires, and nothing else.
            self._connection.execute(
                "UPDATE board_tasks SET status = 'open', archived_at = NULL, "
                "swarm_run_id = ?, updated_at = ? "
                "WHERE id = ? AND (status IN ('done','blocked','cancelled') "
                "                  OR archived_at IS NOT NULL "
                "                  OR swarm_run_id IS NOT ?)",
                (PUMP_LEDGER_RUN_ID, now, task_id, PUMP_LEDGER_RUN_ID),
            )
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return task_id

    @_serialized
    def pump_attempts(self, board_task_id: str) -> list[dict[str, Any]]:
        """Pump-written attempt rows for a lane card, oldest-first.

        Restricted to ``verdict_hash IS NOT NULL`` on purpose — see migration
        109: the scheduler executor's own rows must not tighten the pump cap,
        and pump rows must not be diluted by the executor's.
        """
        return _rows(
            self._connection.execute(
                "SELECT * FROM swarm_attempts WHERE board_task_id = ? "
                "AND verdict_hash IS NOT NULL ORDER BY seq ASC",
                (board_task_id,),
            ).fetchall()
        )

    @_serialized
    def last_pump_verdict_hash(self, board_task_id: str) -> str | None:
        """The newest pump verdict fingerprint for a lane, or None."""
        row = self._connection.execute(
            "SELECT verdict_hash FROM swarm_attempts WHERE board_task_id = ? "
            "AND verdict_hash IS NOT NULL ORDER BY seq DESC LIMIT 1",
            (board_task_id,),
        ).fetchone()
        return None if row is None else str(row["verdict_hash"])

    @_serialized
    def reap_live_pump_attempt(self, board_task_id: str) -> bool:
        """Close a live attempt left behind by a killed pump.

        ``open_attempt`` refuses a second live attempt (partial unique index).
        A pump killed mid-dispatch would therefore wedge its own lane forever,
        which is a NEW stall, so the row is settled honestly as ``crashed``
        rather than deleted.
        """
        row = self._connection.execute(
            "SELECT id FROM swarm_attempts WHERE board_task_id = ? AND ended_at IS NULL",
            (board_task_id,),
        ).fetchone()
        if row is None:
            return False
        attempt_id = str(row["id"])
        self._begin()
        try:
            self._connection.execute(
                "UPDATE swarm_attempts SET ended_at = ?, end_reason = 'crashed', "
                "detail = 'reaped: pump died before closing this attempt' "
                "WHERE id = ? AND ended_at IS NULL",
                (utc_now_iso(), attempt_id),
            )
            self._commit()
        except BaseException:
            self._rollback()
            raise
        return True

    # --- events (WP7 metrics input) --------------------------------------------

    @_serialized
    def events_for_run(
        self, swarm_run_id: str, actions: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """``swarm.event`` rows for a run, chronological, with ``payload_json``
        parsed into ``payload``. WP7's source for resize/rate_limit_stall/
        provider_switched history (mean target_n, cooldown-wait, bottlenecks).

        The ``events`` table lives in the same sqlite file as ``swarm_runs``
        (both reachable through ``default_db_path()``), so this reads it
        directly rather than requiring a second ``Store`` handle — compute_metrics
        only needs a ``SwarmDal``.
        """
        sql = "SELECT * FROM events WHERE target_type = 'swarm_run' AND target_id = ? AND type = ?"
        params: list[Any] = [swarm_run_id, SWARM_EVENT_KIND]
        if actions:
            placeholders = ", ".join("?" for _ in actions)
            sql += f" AND action IN ({placeholders})"
            params.extend(actions)
        sql += " ORDER BY id ASC"
        rows = _rows(self._connection.execute(sql, params).fetchall())
        for row in rows:
            try:
                row["payload"] = json.loads(row.pop("payload_json", None) or "{}")
            except (json.JSONDecodeError, TypeError):
                row["payload"] = {}
        return rows


__all__ = [
    "PUMP_LEDGER_RUN_ID",
    "SwarmDal",
    "pump_lane_task_id",
    "pump_verdict_hash",
]


# --- pump CLI seam (109) --------------------------------------------------------
#
# The four pumps are bash. They need exactly two verbs against this DAL, and both
# must go through the SAME python API the scheduler uses — a shell script issuing
# its own INSERT would be a second write path, and a second write path is how
# "built, tested, never wired" happens in this repo.
#
#   pump-gate    "may this lane dispatch?"   (reads what pump-attempt writes)
#   pump-attempt "record that it did"        (open_attempt → usage → close_attempt)
#
# Machine-readable `key=value` stdout, exit 0 on a decision, exit 2 on an error.
# The decision NEVER rides on the exit code alone: a pump running without `set -e`
# would read a crashed helper as "allow", which is precisely the failure mode this
# package exists to remove.


def _pump_body(args: Any) -> str:
    if getattr(args, "verdict_hash", None):
        return ""
    path = getattr(args, "verdict_body_file", None)
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _pump_resolve_hash(args: Any) -> str:
    explicit = getattr(args, "verdict_hash", None)
    if explicit:
        return str(explicit)
    return pump_verdict_hash(_pump_body(args))


def _pump_cmd_gate(args: Any, dal: SwarmDal) -> int:
    """May this lane dispatch? Two refusals, both proofs of non-progress.

    ``--verdict-body-file``/``--verdict-hash`` is OPTIONAL. A pump that knows
    what body it is about to react to (rework, review, verdict) supplies it and
    is refused BEFORE spending the repeat dispatch. A pump whose fingerprint is
    only knowable after the call (sim, whose hash covers the API response) omits
    it and is refused on the *recorded* pair instead. Same rule — "two
    consecutive identical hashes for a lane" — caught one dispatch earlier when
    the information is available one dispatch earlier.
    """
    task_id = dal.ensure_pump_lane(args.lane)
    supplied = bool(getattr(args, "verdict_hash", None) or getattr(args, "verdict_body_file", None))
    incoming = _pump_resolve_hash(args) if supplied else "-"
    rows = dal.pump_attempts(task_id)
    hashes = [str(row.get("verdict_hash") or "") for row in rows]
    previous = hashes[-1] if hashes else None

    from omniagentos.swarm.scheduler import pump_attempt_count, retry_cap_exceeded

    decision, reason = "allow", "-"
    if supplied and previous is not None and incoming == previous:
        decision, reason = "blocked", "verdict-repeat"
    elif len(hashes) >= 2 and hashes[-1] == hashes[-2]:
        decision, reason = "blocked", "verdict-repeat"
    elif retry_cap_exceeded(dal, task_id, retry_cap=int(args.retry_cap)):
        decision, reason = "blocked", "retry-cap"
    print(
        f"GATE lane={args.lane} task={task_id} attempts={pump_attempt_count(dal, task_id)} "
        f"recorded={len(rows)} verdict_hash={incoming} previous_hash={previous or '-'} "
        f"decision={decision} reason={reason}"
    )
    return 0


def _pump_cmd_attempt(args: Any, dal: SwarmDal) -> int:
    task_id = dal.ensure_pump_lane(args.lane)
    dal.reap_live_pump_attempt(task_id)
    verdict = _pump_resolve_hash(args)
    attempt = dal.open_attempt(
        PUMP_LEDGER_RUN_ID,
        task_id,
        provider=args.provider,
        model=args.model,
        tier=args.pump,
        source=args.source,
        verdict_hash=verdict,
    )
    attempt_id = str(attempt["id"])
    if args.wall_ms is not None:
        dal.record_attempt_usage(attempt_id, wall_ms=int(args.wall_ms), usage_source="pump")
    # --keep-open is the live-dispatch case: the row stays live for as long as
    # the agent runs, so a concurrent gate SEES the in-flight dispatch instead
    # of counting it only after the fact. `pump-close` settles it with the real
    # outcome; a pump killed mid-flight is reaped on the lane's next open.
    end_reason = "-" if args.keep_open else args.end_reason
    if not args.keep_open:
        dal.close_attempt(attempt_id, args.end_reason, args.detail)
    print(
        f"ATTEMPT id={attempt_id} lane={args.lane} task={task_id} "
        f"seq={attempt['seq']} verdict_hash={verdict} end_reason={end_reason}"
    )
    return 0


def _pump_cmd_close(args: Any, dal: SwarmDal) -> int:
    if args.wall_ms is not None:
        dal.record_attempt_usage(args.attempt_id, wall_ms=int(args.wall_ms), usage_source="pump")
    closed = dal.close_attempt(args.attempt_id, args.end_reason, args.detail)
    print(
        f"CLOSED id={args.attempt_id} end_reason={args.end_reason} "
        f"changed={'yes' if closed else 'no'}"
    )
    return 0


def _pump_main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys

    from omniagentos.contracts import default_db_path

    parser = argparse.ArgumentParser(prog="omniagentos.swarm.dal")
    parser.add_argument("--db", default=None, help="sqlite path (default: OMNIAGENTOS_DB)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("pump-gate", "pump-attempt"):
        p = sub.add_parser(name)
        p.add_argument("--lane", required=True)
        p.add_argument("--verdict-body-file", default=None)
        p.add_argument("--verdict-hash", default=None)
        if name == "pump-gate":
            p.add_argument("--retry-cap", default=2)
        else:
            p.add_argument("--pump", required=True)
            p.add_argument("--provider", default="pump")
            p.add_argument("--model", default="pump")
            p.add_argument("--end-reason", default="completed", choices=ATTEMPT_END_REASONS)
            p.add_argument("--detail", default="")
            p.add_argument("--wall-ms", default=None)
            p.add_argument("--source", default="real", choices=("real", "simulated", "test"))
            p.add_argument("--keep-open", action="store_true")

    p_close = sub.add_parser("pump-close")
    p_close.add_argument("--attempt-id", required=True)
    p_close.add_argument("--end-reason", default="completed", choices=ATTEMPT_END_REASONS)
    p_close.add_argument("--detail", default="")
    p_close.add_argument("--wall-ms", default=None)

    p_hash = sub.add_parser("pump-hash")
    p_hash.add_argument("--verdict-body-file", default=None)
    p_hash.add_argument("--verdict-hash", default=None)

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.cmd == "pump-hash":
        print(_pump_resolve_hash(args))
        return 0

    db_path = args.db or default_db_path()
    dal = SwarmDal(db_path)
    try:
        if args.cmd == "pump-gate":
            return _pump_cmd_gate(args, dal)
        if args.cmd == "pump-close":
            return _pump_cmd_close(args, dal)
        return _pump_cmd_attempt(args, dal)
    except Exception as exc:  # noqa: BLE001 -- the pump must SEE the failure, not infer it.
        print(f"PUMP-LEDGER-ERROR cmd={args.cmd} error={exc}", file=sys.stderr)
        return 2
    finally:
        dal.close()


if __name__ == "__main__":  # pragma: no cover -- shell seam
    raise SystemExit(_pump_main())
