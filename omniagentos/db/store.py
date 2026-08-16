"""SQLite implementation of the frozen :class:`omniagentos.contracts.Store` seam."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import weakref
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from threading import RLock, local
from typing import Any, cast

from omniagentos.budget import token_pricing
from omniagentos.contracts import CostObservation, new_id, utc_now_iso
from omniagentos.db.busy import is_busy_or_locked, run_with_busy_retry

_LOG = logging.getLogger(__name__)

# Production SQL for L-10 hot lookups (migration 071). Exported so query-plan
# tests assert against the same strings the store executes — no table-name
# fallbacks, no parallel "test SQL".
SQL_GET_APPROVAL_FOR = (
    "SELECT * FROM approvals WHERE run_id = ? AND step_seq IS ? "
    "ORDER BY CASE state "
    "  WHEN 'pending' THEN 0 "
    "  WHEN 'approved' THEN 1 "
    "  WHEN 'rejected' THEN 2 "
    "  ELSE 3 END, "
    "created_at DESC, id DESC LIMIT 1"
)
SQL_IDEM_FOR_RUN = "SELECT * FROM idempotency WHERE run_id = ? ORDER BY created_at ASC, key ASC"
# Mirrors omniagentos/swarm/dal.py attempt-by-session lookup (L10 call site;
# L14 owns the covering index). Tests pin both the string and the source file.
SQL_SWARM_ATTEMPT_BY_SESSION = (
    "SELECT * FROM swarm_attempts WHERE session_id = ? ORDER BY started_at DESC, seq DESC LIMIT 1"
)

#: How often :meth:`SqliteStore.maybe_checkpoint_wal` is allowed to fire. Per-thread
#: reader connections each pin their own WAL snapshot, so the WAL stops being
#: truncated by SQLite's own auto-checkpoint as soon as reads spread across
#: threads. The tick is best-effort and NEVER blocks (see ``checkpoint_wal``).
_WAL_CHECKPOINT_INTERVAL_S = 60.0

# ``PRAGMA journal_mode=WAL`` does not reliably honor SQLite's busy handler.
# Concurrent first-start processes can therefore fail before the migration
# runner reaches its serialized ``BEGIN IMMEDIATE``. Keep this handshake retry
# private and tightly bounded; general transaction retry belongs to the shared
# reliability primitive rather than this connection bootstrap.
_WAL_INIT_RETRY_TIMEOUT_S = 5.0
_WAL_INIT_RETRY_INTERVAL_S = 0.01

# Slack delivery is bounded well below this. A process that dies after claiming
# an alert therefore suppresses duplicates briefly, then self-heals.
PROVIDER_ALERT_CLAIM_TTL_SECONDS = 60


def _env_int(name: str, default: int, *, minimum: int) -> int:
    """Read a bounded integer setting without making a bad env break startup."""
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


# Queue aging is intentionally process-configurable for operators and tests. Every
# full interval reduces the effective numeric priority by one until the floor; the
# stored priority remains unchanged and auditable. A positive interval prevents a
# divide-by-zero query, while the default floor preserves the shared 0..3 taxonomy.
AGING_INTERVAL_SECONDS = _env_int(
    "OMNIAGENTOS_RUN_AGING_INTERVAL_SECONDS",
    _env_int("AGING_INTERVAL_SECONDS", 900, minimum=1),
    minimum=1,
)
AGING_PRIORITY_FLOOR = min(
    3,
    _env_int(
        "OMNIAGENTOS_RUN_AGING_PRIORITY_FLOOR",
        _env_int("AGING_PRIORITY_FLOOR", 0, minimum=0),
        minimum=0,
    ),
)

_RUN_COLUMNS = frozenset(
    {
        "id",
        "task_id",
        "discipline_id",
        "arm",
        "harness",
        "harness_version",
        "env_hash",
        "harness_params",
        "agent",
        "model",
        "attempt",
        "state",
        "priority",
        "worker_id",
        "plan_json",
        "output_text",
        "output_json",
        "error",
        "wall_ms",
        "turns",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "usage_estimated",
        "usage_source",
        # Migration 095: Kimi/Waku cache and gateway telemetry. Keeping these
        # in the DAL allow-list makes the migrated schema production-writable
        # rather than a synthetic test-only table shape.
        "cached_tokens_read",
        "cached_tokens_written",
        "cache_miss_tokens",
        "gateway_source",
        "hourly_turn_count",
        "budget_json",
        "cancel_requested",
        "trace_id",
        "session_ref",
        "scope_json",
        # Migration 059 adds these alongside scope_json; without them
        # update_run(rid, {"lease_generation": n}) raises "unknown columns" and the
        # Phase 3 lane wiring cannot fence a run at all.
        "lease_generation",
        "lease_expires_at",  # 059: declared ScopeClaim set (crash-resume replays it)
        "manifest_path",
        "vault_note_path",
        "queued_at",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
        # Migration 058: denormalized from tasks so the budget fan-out at
        # runner/core.py:1760 can add a project:<id> scope without a second
        # query per usage record, and so every evaluation query is
        # project-scopable (idx_runs_project). NSC-C39-04.
        "project_id",
    }
)
_STEP_COLUMNS = frozenset(
    {
        "name",
        "action_class",
        "status",
        "checkpoint_json",
        "result_json",
        "error",
        "idempotency_key",
        "started_at",
        "finished_at",
    }
)
_TASK_COLUMNS = frozenset(
    {
        "id",
        "discipline_id",
        "project_id",
        "title",
        "input_json",
        "acceptance_json",
        "state",
        "risk",
        "deadline",
        "created_at",
        "updated_at",
    }
)
_APPROVAL_COLUMNS = frozenset(
    {
        "id",
        "run_id",
        "task_id",
        "step_seq",
        "action_class",
        "proposed_action",
        "params_json",
        "risk",
        "evidence",
        "state",
        "decided_by",
        "decision_note",
        "decided_at",
        "expires_at",
        "created_at",
        "session_id",  # W1: nullable link to sessions(id) for Session-Bridge approvals (additive)
    }
)
_ARTIFACT_COLUMNS = frozenset({"id", "run_id", "type", "uri", "sha256", "bytes", "created_at"})
_DISCIPLINE_COLUMNS = frozenset({"id", "name", "metric_contract", "status", "created_at"})

_PROVIDER_CALL_COLUMNS = (
    "call_id",
    "request_id",
    "execution_id",
    "run_id",
    "campaign_id",
    "reservation_id",
    "task_id",
    "attempt_id",
    "session_id",
    "work_id",
    "root_trace_id",
    "stage",
    "attempt_index",
    "provider",
    "transport",
    "requested_model",
    "effective_model",
    "model_lineage",
    "billing_provider",
    "adapter_key",
    "provider_request_id",
    "request_state",
    "provider_outcome",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd_decimal",
    "cost_usd_nanos",
    "cost_upper_bound_usd_nanos",
    "cost_quality",
    "cost_source",
    "pricing_revision",
    "created_at",
    "settled_at",
)
_PROVIDER_CALL_IMMUTABLE_COLUMNS = frozenset(
    {
        "call_id",
        "request_id",
        "execution_id",
        "run_id",
        "campaign_id",
        "reservation_id",
        "task_id",
        "attempt_id",
        "session_id",
        "work_id",
        "root_trace_id",
        "stage",
        "attempt_index",
        "provider",
        "transport",
        "requested_model",
        "effective_model",
        "model_lineage",
        "billing_provider",
        "adapter_key",
        "created_at",
    }
)
_PROVIDER_CALL_FILTERS = frozenset({"run_id", "execution_id", "campaign_id", "reservation_id"})

# ``spend_day`` is intentionally a query-layer projection rather than a schema
# object.  It keeps the append-only migration inventory untouched while giving
# every consumer (the guard and health-sentinel) one literal definition of daily
# billed spend.  Canonical ``created_at`` values are UTC ``...Z`` strings, so
# their first ten bytes are the UTC calendar day.
SPEND_DAY_SQL = (
    "SELECT billing_provider AS provider, substr(created_at, 1, 10) AS utc_day, "
    "SUM(COALESCE(cost_usd_nanos, cost_upper_bound_usd_nanos, 0)) AS spend_usd_nanos, "
    "COUNT(*) AS ledger_row_count "
    "FROM provider_call_usage "
    "GROUP BY billing_provider, substr(created_at, 1, 10)"
)

# Blocker-1 fix (2026-08-06 review): the canonical Moonshot billing identity
# is "moonshot" (it matches the live estate kimi-shim co-writer and the
# provider-sentinel paid-snapshot roster). A pre-fix build of this guard
# briefly used "kimi" as the billing_provider string for the same real
# provider. Any row written under either string must count toward the SAME
# daily cap -- two independent enforcers reading disjoint sub-totals is
# exactly the double-spend Blocker 1 identified. This alias table is a
# dual-read shim, not a rename of history: existing rows are never mutated.
PROVIDER_BILLING_ALIASES: dict[str, tuple[str, ...]] = {
    "moonshot": ("moonshot", "kimi"),
}


def _billing_provider_aliases(provider: str) -> tuple[str, ...]:
    """Every billing_provider string that must be summed for ``provider``."""

    return PROVIDER_BILLING_ALIASES.get(provider, (provider,))


def _writer[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Serialize a mutating method behind the store's process-wide writer lock.

    This is the pre-Phase-2 ``_serialized`` behaviour, unchanged and deliberately
    conservative: SQLite admits exactly one writer at a time anyway, and sixteen
    call sites outside this module reach in and hold ``store._lock`` themselves
    while driving raw SQL through ``store._connection``. Keeping one real lock
    for every write keeps those sites correct without auditing each one.
    """

    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        store = cast("SqliteStore", args[0])
        with store._lock:
            return method(*args, **kwargs)

    return wrapped


def _reader[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Run a pure-SELECT method on the CALLING thread's connection, off the writer lock.

    WAL gives every connection its own read snapshot, so a reader is never blocked
    by the single writer. That is the whole point of Phase 2: a worker sitting
    inside ``BEGIN IMMEDIATE`` must not stall the SSE feed and the API.

    Readers still take ``_read_lock`` -- a DIFFERENT lock from the writer's -- and
    that is a measured decision, not caution. On CPython the ``sqlite3`` module
    releases the GIL around every step and SQLite's global allocator/page-cache
    mutexes are shared by ALL connections in the process, so N threads inside
    SQLite at once collapse superlinearly. Measured on this repo (macOS/arm64,
    CPython 3.12.12, SQLite 3.50.4), N threads each on their OWN connection
    running the same SELECT:

        1 thread  ~6,200 reads/s | 2 ~3,250/s | 4 ~1,190/s | 8 ~150/s

    The same collapse reproduces with raw ``sqlite3``, on SEPARATE database files,
    and even for ``SELECT 1`` -- it is a CPython/SQLite process-global property,
    nothing to do with this store. Admitting one reader at a time is therefore
    both the fastest configuration available AND the one that keeps p99 flat.

    What changed versus pre-Phase-2 is WHO a reader waits for: another reader for
    microseconds, never a writer for the length of its transaction. Under an
    8-reader load with a writer holding BEGIN IMMEDIATE for 250 ms, read latency
    went from p95 255 ms (i.e. the full write) to p95 2.0 ms.

    If a future runtime removes the process-global contention -- a free-threaded
    build, or one configured with ``SQLITE_CONFIG_MEMSTATUS=0`` -- delete the
    ``_read_lock`` acquisition here and readers become genuinely parallel. Nothing
    else in the store depends on readers being serialized.

    ``:memory:`` stores have no per-thread connections to spread over -- one
    in-memory database is shared by every thread -- so they keep taking the
    writer lock and stay exactly as serialized as they were before.
    """

    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        store = cast("SqliteStore", args[0])
        if store._shared_connection is not None:
            with store._lock:
                return method(*args, **kwargs)
        with store._read_lock:
            store._connection  # noqa: B018 -- resolve/open this thread's connection
            return method(*args, **kwargs)

    return wrapped


class _ConnectionHolder:
    """Weak-referenceable owner of one thread's connection.

    ``sqlite3.Connection`` does not support weak references, so the store tracks
    holders instead. A holder is reachable only from the owning thread's
    ``threading.local`` slot: when that thread exits, the holder is collected,
    the connection's last strong reference goes with it, and SQLite closes the
    handle. Without this indirection a long-lived store would accumulate one
    leaked file descriptor per retired worker thread.
    """

    __slots__ = ("connection", "__weakref__")

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection


def _enable_wal(
    connection: sqlite3.Connection,
    *,
    timeout_s: float = _WAL_INIT_RETRY_TIMEOUT_S,
    retry_interval_s: float = _WAL_INIT_RETRY_INTERVAL_S,
) -> None:
    """Enable WAL, waiting a bounded time for concurrent startup handshakes.

    SQLite's busy timeout is configured before this call, but the
    ``journal_mode`` pragma can still raise BUSY immediately. A structured log
    metric records retry count/wait time whenever that contention occurs.
    """
    started = time.monotonic()
    deadline = started + max(0.0, timeout_s)
    retries = 0
    while True:
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as error:
            if not is_busy_or_locked(error):
                raise
            retries += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _LOG.error(
                    "sqlite_wal_init_contention_exhausted retries=%d wait_ms=%d",
                    retries,
                    round((time.monotonic() - started) * 1000),
                )
                raise
            time.sleep(min(max(0.0, retry_interval_s), remaining))
            continue

        if retries:
            _LOG.warning(
                "sqlite_wal_init_contention retries=%d wait_ms=%d",
                retries,
                round((time.monotonic() - started) * 1000),
            )
        return


def _connect(db_path: str) -> sqlite3.Connection:
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
        _enable_wal(connection)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection
    except BaseException:
        connection.close()
        raise


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _checked_fields(fields: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    unknown = fields.keys() - allowed
    if unknown:
        raise ValueError(f"unknown columns: {', '.join(sorted(unknown))}")
    return fields


def _insert_sql(table: str, fields: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    return f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(fields.values())


def _next_event_sequence(connection: sqlite3.Connection, execution_id: str) -> int:
    """Gap-free, race-safe next ``events.sequence`` for ``execution_id``.

    W2.6 (migration 086): every event belonging to one lane execution (a run,
    a session, a longhaul task attempt, ...) gets a dense 1, 2, 3, ... counter
    scoped to its ``execution_id``, alongside the global autoincrement ``id``.

    Callers MUST invoke this inside the same BEGIN IMMEDIATE write transaction
    that performs the INSERT, on the SAME connection, before committing. SQLite
    takes a RESERVED lock at BEGIN IMMEDIATE that blocks every other writer
    (any thread, any process, any connection to this database file) from
    starting its own write transaction until this one commits or rolls back --
    that is what makes the read-then-insert here atomic across the whole
    events table, not just within one Python-level lock. Shared by every
    insert_event call site (``SqliteStore``, ``SessionsDal``, longhaul's
    ``LonghaulEngine._event_in_tx``) so the guarantee is defined once.
    """
    row = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE execution_id = ?",
        (execution_id,),
    ).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 1


def _where(filters: dict[str, Any], allowed: frozenset[str]) -> tuple[str, list[Any]]:
    _checked_fields(filters, allowed)
    if not filters:
        return "", []
    clauses: list[str] = []
    values: list[Any] = []
    for column, value in filters.items():
        if value is None:
            clauses.append(f"{column} IS NULL")
        else:
            clauses.append(f"{column} = ?")
            values.append(value)
    return " WHERE " + " AND ".join(clauses), values


def _provider_call_payload(
    observation: CostObservation | dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate one provider-call payload at the DAL boundary.

    A replayed plain dict may omit Pydantic's generated timestamps. Reusing the
    already-persisted timestamp keeps that otherwise-identical replay stable.
    """

    candidate = (
        observation.model_dump(mode="json")
        if isinstance(observation, CostObservation)
        else dict(observation)
    )
    if existing is not None and "created_at" not in candidate:
        candidate["created_at"] = existing["created_at"]
    # Once-only pricing (NSG-008 F1) belongs to the ONE writer whose cost
    # columns this DAL derives for itself: the estate kimi shim, priced below
    # by ``_price_kimi_shim_observation``. Once such a row has been priced --
    # its ``cost_source`` no longer carries the raw ``estate-kimi-shim:``
    # placeholder -- a replay or settlement must reuse the stored price instead
    # of re-deriving it from a registry that may have moved since.
    #
    # The ``adapter_key`` term is load-bearing and mirrors the domain check
    # that opens ``_price_kimi_shim_observation``. Without it this freeze also
    # hit writers that supply their own cost provenance, silently DISCARDING a
    # deliberate stamp: the spend guard's alert-claim release
    # (``spend-cap-alert:claim-failed``) stayed readable as an active claim and
    # suppressed the next soft-threshold alert, and swept/unavailable
    # settlements lost ``...:stale-reservation-sweep`` /
    # ``...:provider-cost-unavailable``.
    existing_source = existing.get("cost_source") if existing is not None else None
    if (
        existing is not None
        and candidate.get("adapter_key") == "estate-kimi-shim"
        and isinstance(existing_source, str)
        and not existing_source.startswith("estate-kimi-shim:")
        and candidate.get("cost_quality") != "exact"
    ):
        for column in (
            "cost_usd_decimal",
            "cost_usd_nanos",
            "cost_upper_bound_usd_nanos",
            "cost_quality",
            "cost_source",
            "pricing_revision",
        ):
            candidate[column] = existing[column]
    candidate = _price_kimi_shim_observation(candidate)
    validated = CostObservation.model_validate(candidate)
    payload = validated.model_dump(mode="json")
    return {column: payload[column] for column in _PROVIDER_CALL_COLUMNS}


def _price_kimi_shim_observation(candidate: dict[str, Any]) -> dict[str, Any]:
    """Replace the estate shim's raw placeholder with registry-derived truth once."""

    if candidate.get("adapter_key") != "estate-kimi-shim":
        return candidate
    if candidate.get("cost_quality") == "exact":
        return candidate
    cost_source = candidate.get("cost_source")
    if isinstance(cost_source, str) and not cost_source.startswith("estate-kimi-shim:"):
        return candidate

    priced = dict(candidate)
    input_tokens = priced.get("input_tokens")
    output_tokens = priced.get("output_tokens")
    has_input = (
        isinstance(input_tokens, int)
        and not isinstance(input_tokens, bool)
        and input_tokens >= 0
    )
    has_output = (
        isinstance(output_tokens, int)
        and not isinstance(output_tokens, bool)
        and output_tokens >= 0
    )
    if has_input != has_output:
        missing_side = "output" if has_input else "input"
        priced["input_tokens"] = input_tokens if has_input else None
        priced["output_tokens"] = output_tokens if has_output else None
        priced["cost_usd_decimal"] = None
        priced["cost_usd_nanos"] = None
        priced["cost_quality"] = "unknown"
        priced["cost_upper_bound_usd_nanos"] = None
        priced["cost_source"] = f"modelintel:unpriced:partial-tokens:missing-{missing_side}"
        return priced

    model = priced.get("effective_model") or priced.get("requested_model")
    estimate = token_pricing.estimate_token_cost(
        model,
        priced.get("input_tokens"),
        priced.get("output_tokens"),
    )
    priced["cost_usd_decimal"] = None
    priced["cost_usd_nanos"] = None
    if estimate is None:
        provider = priced.get("billing_provider") or priced.get("provider") or "unknown"
        model_name = model or "unknown"
        priced["cost_quality"] = token_pricing.cost_quality(None, None)
        priced["cost_upper_bound_usd_nanos"] = None
        priced["cost_source"] = f"modelintel:unpriced:{provider}/{model_name}"
        return priced

    priced["cost_quality"] = token_pricing.cost_quality(None, estimate.cost_usd)
    priced["cost_upper_bound_usd_nanos"] = round(estimate.cost_usd * 1_000_000_000)
    priced["cost_source"] = estimate.source
    return priced


def _provider_call_equal(row: dict[str, Any], payload: dict[str, Any]) -> bool:
    return all(row[column] == payload[column] for column in _PROVIDER_CALL_COLUMNS)


def _get_provider_call_row(connection: sqlite3.Connection, call_id: str) -> dict[str, Any] | None:
    return _row(
        connection.execute(
            "SELECT * FROM provider_call_usage WHERE call_id = ?",
            (call_id,),
        ).fetchone()
    )


def _insert_provider_call_row(
    connection: sqlite3.Connection, payload: dict[str, Any]
) -> dict[str, Any]:
    columns = ", ".join(_PROVIDER_CALL_COLUMNS)
    placeholders = ", ".join("?" for _ in _PROVIDER_CALL_COLUMNS)
    connection.execute(
        f"INSERT INTO provider_call_usage ({columns}) VALUES ({placeholders})",
        tuple(payload[column] for column in _PROVIDER_CALL_COLUMNS),
    )
    row = _get_provider_call_row(connection, str(payload["call_id"]))
    assert row is not None
    return row


def _record_provider_call_row(
    connection: sqlite3.Connection,
    observation: CostObservation | dict[str, Any],
) -> dict[str, Any]:
    raw_call_id = (
        observation.call_id
        if isinstance(observation, CostObservation)
        else observation.get("call_id")
    )
    existing = (
        _get_provider_call_row(connection, str(raw_call_id))
        if isinstance(raw_call_id, str) and raw_call_id
        else None
    )
    payload = _provider_call_payload(observation, existing=existing)
    if existing is None:
        return _insert_provider_call_row(connection, payload)
    if _provider_call_equal(existing, payload):
        return existing
    raise ValueError(f"provider call {payload['call_id']!r} replay conflicts with stored payload")


def _settle_provider_call_row(
    connection: sqlite3.Connection,
    call_id: str,
    settlement: CostObservation | dict[str, Any],
) -> dict[str, Any]:
    """Settle an initiated row once; an identical settlement is a no-op."""

    existing = _get_provider_call_row(connection, call_id)
    if existing is None:
        raise KeyError(f"unknown provider call: {call_id}")
    updates = (
        settlement.model_dump(mode="json")
        if isinstance(settlement, CostObservation)
        else dict(settlement)
    )
    if "call_id" in updates and updates["call_id"] != call_id:
        raise ValueError("settlement call_id does not match target call")
    for column in _PROVIDER_CALL_IMMUTABLE_COLUMNS:
        if column in updates and updates[column] != existing[column]:
            raise ValueError(
                f"provider call {call_id!r} settlement changes immutable field {column!r}"
            )

    if existing["settled_at"] is not None:
        candidate = dict(existing)
        candidate.update(updates)
        candidate["call_id"] = call_id
        candidate["settled_at"] = existing["settled_at"]
        payload = _provider_call_payload(candidate, existing=existing)
        if _provider_call_equal(existing, payload):
            return existing
        raise ValueError(f"provider call {call_id!r} already has a conflicting settlement")

    candidate = {column: existing[column] for column in _PROVIDER_CALL_COLUMNS}
    candidate.update(updates)
    candidate["call_id"] = call_id
    candidate["created_at"] = existing["created_at"]
    candidate["settled_at"] = updates.get("settled_at") or utc_now_iso()
    payload = _provider_call_payload(candidate, existing=existing)
    assignments = ", ".join(
        f"{column} = ?" for column in _PROVIDER_CALL_COLUMNS if column != "call_id"
    )
    connection.execute(
        f"UPDATE provider_call_usage SET {assignments} WHERE call_id = ?",
        (
            *(payload[column] for column in _PROVIDER_CALL_COLUMNS if column != "call_id"),
            call_id,
        ),
    )
    row = _get_provider_call_row(connection, call_id)
    assert row is not None
    return row


def _provider_call_where(filters: dict[str, str | None]) -> tuple[str, list[Any]]:
    return _where(
        {key: value for key, value in filters.items() if value is not None},
        _PROVIDER_CALL_FILTERS,
    )


def _nano_usd_decimal(nanos: int) -> str:
    whole, fractional = divmod(nanos, 1_000_000_000)
    if fractional == 0:
        return str(whole)
    return f"{whole}.{fractional:09d}".rstrip("0")


def _aggregate_provider_call_cost_rows(
    connection: sqlite3.Connection,
    *,
    run_id: str | None = None,
    execution_id: str | None = None,
    campaign_id: str | None = None,
    reservation_id: str | None = None,
) -> dict[str, Any]:
    """Project exact/estimated/unknown rows without treating NULL as zero."""

    where, parameters = _provider_call_where(
        {
            "run_id": run_id,
            "execution_id": execution_id,
            "campaign_id": campaign_id,
            "reservation_id": reservation_id,
        }
    )
    row = connection.execute(
        "SELECT COUNT(*) AS call_count, "
        "SUM(CASE WHEN cost_quality = 'exact' THEN 1 ELSE 0 END) AS exact_count, "
        "SUM(CASE WHEN cost_quality = 'estimated' THEN 1 ELSE 0 END) AS estimated_count, "
        "SUM(CASE WHEN cost_quality = 'unknown' THEN 1 ELSE 0 END) AS unknown_count, "
        "SUM(CASE WHEN cost_quality = 'exact' THEN cost_usd_nanos END) AS known_nanos, "
        "SUM(CASE WHEN cost_quality = 'estimated' "
        "         THEN cost_upper_bound_usd_nanos END) AS estimated_nanos "
        f"FROM provider_call_usage{where}",
        parameters,
    ).fetchone()
    assert row is not None
    call_count = int(row["call_count"])
    exact_count = int(row["exact_count"] or 0)
    estimated_count = int(row["estimated_count"] or 0)
    unknown_count = int(row["unknown_count"] or 0)
    known_nanos = int(row["known_nanos"] or 0) if exact_count else None
    estimated_nanos = int(row["estimated_nanos"] or 0) if estimated_count else None

    if unknown_count and (exact_count or estimated_count):
        quality = "mixed"
    elif unknown_count:
        quality = "unknown"
    elif exact_count and estimated_count:
        quality = "mixed"
    elif exact_count:
        quality = "exact"
    elif estimated_count:
        quality = "estimated"
    else:
        quality = "unknown"

    accounting_incomplete = call_count == 0 or unknown_count > 0
    chargeable_nanos = None
    if not accounting_incomplete:
        chargeable_nanos = (known_nanos or 0) + (estimated_nanos or 0)
    return {
        "known_usd": None if known_nanos is None else known_nanos / 1_000_000_000,
        "known_usd_decimal": (None if known_nanos is None else _nano_usd_decimal(known_nanos)),
        "estimated_upper_bound_usd": (
            None if estimated_nanos is None else estimated_nanos / 1_000_000_000
        ),
        "chargeable_usd": (None if chargeable_nanos is None else chargeable_nanos / 1_000_000_000),
        "unknown_call_count": unknown_count,
        "quality": quality,
        "safe_to_compare": not accounting_incomplete,
        "accounting_incomplete": accounting_incomplete,
        "call_count": call_count,
    }


def _provider_spend_day_rows(
    connection: sqlite3.Connection,
    *,
    provider: str | None = None,
    utc_day: str | None = None,
) -> list[dict[str, Any]]:
    """Return the ``spend_day`` projection, optionally narrowed by identity/day."""

    clauses: list[str] = []
    parameters: list[Any] = []
    if provider is not None:
        clauses.append("provider = ?")
        parameters.append(provider)
    if utc_day is not None:
        clauses.append("utc_day = ?")
        parameters.append(utc_day)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = connection.execute(
        f"SELECT * FROM ({SPEND_DAY_SQL}){where} ORDER BY utc_day, provider",
        parameters,
    ).fetchall()
    return _rows(rows)


def _provider_spend_nanos(
    connection: sqlite3.Connection,
    *,
    provider: str,
    utc_day: str,
) -> int:
    """Cap-relevant daily spend for ``provider``, summed across billing aliases.

    Used only for the reservation's own headroom math (Blocker 1): a row
    written under any alias of ``provider`` (e.g. a legacy ``kimi`` row for
    the canonical ``moonshot`` identity, or a live co-writer's row) counts
    exactly once toward the same cap. ``spend_day`` reporting reads
    (``_provider_spend_day_rows``) stay literal/per-string so operators can
    still see which writer produced which rows.
    """

    aliases = _billing_provider_aliases(provider)
    placeholders = ",".join("?" for _ in aliases)
    row = connection.execute(
        "SELECT SUM(COALESCE(cost_usd_nanos, cost_upper_bound_usd_nanos, 0)) AS spend_usd_nanos "
        f"FROM provider_call_usage WHERE billing_provider IN ({placeholders}) "
        "AND substr(created_at, 1, 10) = ?",
        (*aliases, utc_day),
    ).fetchone()
    if row is None:
        return 0
    return int(row["spend_usd_nanos"] or 0)


class SqliteStore:
    """Auto-migrated, thread-safe SQLite store with one connection per thread.

    ``_connection`` is a *property*, not an attribute: it resolves to the calling
    thread's own ``sqlite3.Connection``, opened on first use. Never bind it to an
    instance attribute -- ``check_same_thread=False`` means SQLite will not warn
    you, so a cached handle silently interleaves one thread's statements into
    another thread's transaction.

    Writes stay serialized behind ``_lock`` (see :func:`_writer`). Reads never
    touch ``_lock`` -- they are admitted through the separate ``_read_lock`` and
    rely on WAL snapshot isolation, so a reader is never delayed by a writer's
    open transaction. :func:`_reader` carries the measurements behind that split.

    ``:memory:`` is the one exception. Each connection to ``":memory:"`` is a
    *distinct, empty* database, so a per-thread registry there would hand every
    thread its own blank schema. Such stores keep a single shared connection and
    lock every method, exactly as before Phase 2.
    """

    def __init__(self, db_path: str, *, wal_checkpoint_interval_s: float | None = None) -> None:
        self._lock = RLock()
        # Reader admission, deliberately NOT the writer lock. Reentrant so a
        # writer that calls a reader (set_pause -> get_pause) and any external
        # `with store._lock:` block that then calls a read method both work.
        # Lock order is always _lock -> _read_lock; nothing takes them the other
        # way round, so the pair cannot deadlock. See :func:`_reader`.
        self._read_lock = RLock()
        self._db_path = db_path
        self._closed = False
        self._local = local()
        self._holders: weakref.WeakSet[_ConnectionHolder] = weakref.WeakSet()
        self._holders_lock = RLock()
        self._wal_checkpoint_interval_s = (
            _WAL_CHECKPOINT_INTERVAL_S
            if wal_checkpoint_interval_s is None
            else wal_checkpoint_interval_s
        )
        self._last_wal_checkpoint = time.monotonic()

        connection = _connect(db_path)
        try:
            # Local import avoids a module cycle: migrate uses the shared
            # connection factory defined above.
            from omniagentos.db.migrate import migrate_connection

            migrate_connection(connection)
        except BaseException:
            connection.close()
            raise
        # ":memory:" cannot be reopened, so it pins the migrated handle for every
        # thread. A file-backed store hands the migrated handle to the
        # constructing thread and opens a fresh one per additional thread.
        self._shared_connection: sqlite3.Connection | None = (
            connection if db_path == ":memory:" else None
        )
        self._adopt(connection)

    # --- connection registry ---
    def _adopt(self, connection: sqlite3.Connection) -> _ConnectionHolder:
        holder = _ConnectionHolder(connection)
        self._local.holder = holder
        with self._holders_lock:
            self._holders.add(holder)
        return holder

    @property
    def _connection(self) -> sqlite3.Connection:
        """This thread's connection, opened on demand. Resolve it, never cache it."""
        if self._closed:
            raise RuntimeError("SqliteStore is closed")
        shared = self._shared_connection
        if shared is not None:
            return shared
        holder: _ConnectionHolder | None = getattr(self._local, "holder", None)
        if holder is None:
            # Migrations already ran on the constructing thread; this only
            # reapplies the per-connection pragmas (busy_timeout, foreign_keys,
            # synchronous), which are NOT inherited across connections.
            holder = self._adopt(_connect(self._db_path))
        return holder.connection

    def close(self) -> None:
        """Close every connection this store has handed out, from any thread.

        Idempotent. Subsequent ``_connection`` access raises rather than silently
        reopening, so a use-after-close surfaces at the seam.
        """
        # _lock then _read_lock: the one order this store ever takes them in, so
        # close() cannot pull a connection out from under an in-flight read.
        with self._lock, self._read_lock:
            if self._closed:
                return
            self._closed = True
            with self._holders_lock:
                holders = list(self._holders)
                self._holders.clear()
            self._shared_connection = None
            self._local = local()
        for holder in holders:
            try:
                holder.connection.close()
            except sqlite3.Error:  # pragma: no cover -- best-effort teardown
                pass

    # --- WAL maintenance ---
    def checkpoint_wal(self, mode: str = "TRUNCATE") -> tuple[int, int, int]:
        """Run one ``PRAGMA wal_checkpoint`` and return ``(busy, log, checkpointed)``.

        Deliberately NON-blocking: ``busy_timeout`` is dropped to 0 for the
        duration so that a checkpoint contending with a live reader returns
        ``busy=1`` immediately instead of stalling a writer for up to five
        seconds. Checkpointing is maintenance -- skipping one is free, blocking
        the write path is not.
        """
        if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            raise ValueError(f"unknown wal_checkpoint mode: {mode!r}")
        with self._lock:
            connection = self._connection
            connection.execute("PRAGMA busy_timeout=0")
            try:
                row = connection.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
            finally:
                connection.execute("PRAGMA busy_timeout=5000")
        if row is None:  # pragma: no cover -- non-WAL journal modes
            return (1, -1, -1)
        return (int(row[0]), int(row[1]), int(row[2]))

    def maybe_checkpoint_wal(self) -> bool:
        """Checkpoint at most once per ``_wal_checkpoint_interval_s``. Best effort.

        Called from :meth:`_commit`, i.e. always on the writer's thread with the
        writer lock held and no transaction open. ``:memory:`` has no WAL, and a
        non-positive interval disables the tick entirely.
        """
        if self._shared_connection is not None or self._wal_checkpoint_interval_s <= 0:
            return False
        now = time.monotonic()
        if now - self._last_wal_checkpoint < self._wal_checkpoint_interval_s:
            return False
        self._last_wal_checkpoint = now
        try:
            self.checkpoint_wal("TRUNCATE")
        except sqlite3.Error:  # pragma: no cover -- maintenance never fails a write
            return False
        return True

    def _holds_writer_lock(self) -> bool:
        """Whether THIS thread currently owns ``_lock``.

        ``_thread.RLock._is_owned`` is undeclared in typeshed but has been part of
        the C implementation since forever. When the probe is absent the answer
        is unknown — fail closed (False), never report "held". A favourable
        non-result would disable the ``_begin`` isolation guardrail.
        """
        is_owned = getattr(self._lock, "_is_owned", None)
        if is_owned is None:
            return False
        return bool(is_owned())

    def _begin(self) -> None:
        # Guardrail, not an optimisation. Every BEGIN IMMEDIATE must happen under
        # the writer lock: without it two threads on two connections can open two
        # transactions, and the second one's COMMIT lands the first one's writes.
        # Failing here names the offending call site; corrupting silently does not.
        assert self._holds_writer_lock(), (
            "SqliteStore._begin requires the writer lock; wrap the caller in "
            "`with store._lock:` or decorate it with @_writer"
        )
        self._connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._connection.commit()
        self.maybe_checkpoint_wal()

    def _rollback(self) -> None:
        self._connection.rollback()

    def _execute_write_txn[R](self, body: Callable[[sqlite3.Connection], R], *, op: str) -> R:
        """Run ``body`` as one BEGIN IMMEDIATE transaction with M-33 busy retry.

        The process writer lock is taken *inside* each attempt so backoff sleeps
        never pin the lock. ``body`` must not open nested transactions or call
        ``_begin``/``_write`` (those would re-enter BEGIN on the same connection).
        """

        def attempt() -> R:
            with self._lock:
                connection = self._connection
                try:
                    connection.rollback()
                except sqlite3.OperationalError:
                    pass
                connection.execute("BEGIN IMMEDIATE")
                try:
                    result = body(connection)
                    connection.commit()
                    self.maybe_checkpoint_wal()
                    return result
                except BaseException:
                    try:
                        connection.rollback()
                    except sqlite3.OperationalError:
                        pass
                    raise

        return run_with_busy_retry(attempt, op=op)

    # --- runs ---
    @_writer
    def enqueue_run(self, row: dict[str, Any]) -> None:
        values = _checked_fields(row, _RUN_COLUMNS)
        sql, parameters = _insert_sql("runs", values)
        self._write(sql, parameters)

    def claim_next_run(self, worker_id: str) -> dict[str, Any] | None:
        now = utc_now_iso()

        def body(connection: sqlite3.Connection) -> dict[str, Any] | None:
            candidate = connection.execute(
                "SELECT id FROM runs WHERE state = 'queued' "
                "ORDER BY MAX(?, priority - CAST(MAX(0, unixepoch(?) - "
                "unixepoch(queued_at)) / ? AS INTEGER)) ASC, "
                "queued_at ASC, id ASC LIMIT 1",
                (AGING_PRIORITY_FLOOR, now, AGING_INTERVAL_SECONDS),
            ).fetchone()
            if candidate is None:
                return None
            run_id = str(candidate["id"])
            connection.execute(
                "UPDATE runs SET state = 'running', worker_id = ?, "
                "started_at = COALESCE(started_at, ?), updated_at = ? "
                "WHERE id = ? AND state = 'queued'",
                (worker_id, now, now, run_id),
            )
            claimed = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return _row(claimed)

        return self._execute_write_txn(body, op="sqlite_store.claim_next_run")

    def reclaim_stale_runs(self, worker_id: str, stale_s: int) -> list[dict[str, Any]]:
        cutoff = (datetime.now(UTC) - timedelta(seconds=stale_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
        now = utc_now_iso()

        def body(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            stale = connection.execute(
                "SELECT r.id FROM runs AS r LEFT JOIN heartbeats AS h "
                "ON h.worker_id = r.worker_id "
                "WHERE r.state IN ('running', 'awaiting_approval') "
                "AND (h.worker_id IS NULL OR h.last_beat_at < ?) "
                "ORDER BY r.queued_at ASC, r.id ASC",
                (cutoff,),
            ).fetchall()
            run_ids = [str(row["id"]) for row in stale]
            for run_id in run_ids:
                connection.execute(
                    "UPDATE runs SET worker_id = ?, updated_at = ? WHERE id = ?",
                    (worker_id, now, run_id),
                )
            reclaimed = [
                connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
                for run_id in run_ids
            ]
            return _rows(row for row in reclaimed if row is not None)

        return self._execute_write_txn(body, op="sqlite_store.reclaim_stale_runs")

    @_reader
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        )

    @_reader
    def get_runs_by_ids(self, run_ids: list[str]) -> dict[str, dict[str, Any]]:
        unique = list(dict.fromkeys(run_ids))
        if not unique:
            return {}
        # Bounded by live board size; SQLite ≥3.32 var limit 32766; callers degrade gracefully.
        placeholders = ",".join("?" for _ in unique)
        rows = self._connection.execute(
            f"SELECT * FROM runs WHERE id IN ({placeholders})", unique
        ).fetchall()
        return {str(row["id"]): dict(row) for row in rows}

    @_writer
    def update_run(
        self, run_id: str, fields: dict[str, Any], expect_worker: str | None = None
    ) -> bool:
        values = dict(_checked_fields(fields, _RUN_COLUMNS - {"id"}))
        if not values:
            query = "SELECT 1 FROM runs WHERE id = ?"
            parameters: list[Any] = [run_id]
            if expect_worker is not None:
                query += " AND worker_id = ?"
                parameters.append(expect_worker)
            return self._connection.execute(query, parameters).fetchone() is not None
        assignments = ", ".join(f"{column} = ?" for column in values)
        sql = f"UPDATE runs SET {assignments} WHERE id = ?"
        parameters = [*values.values(), run_id]
        if expect_worker is not None:
            sql += " AND worker_id = ?"
            parameters.append(expect_worker)
        return self._write_count(sql, parameters) > 0

    @_reader
    def list_runs(self, filters: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]:
        clause, parameters = _where(filters, _RUN_COLUMNS)
        parameters.append(limit)
        return _rows(
            self._connection.execute(
                f"SELECT * FROM runs{clause} ORDER BY queued_at DESC, id DESC LIMIT ?", parameters
            ).fetchall()
        )

    @_reader
    def list_runs_for_project(
        self, project_id: str, filters: dict[str, Any], limit: int = 100
    ) -> list[dict[str, Any]]:
        """Runs owned (transitively via task) by ``project_id``.

        Project scoping happens in SQL *before* ORDER BY / LIMIT so a project's
        older runs are never hidden behind a global window of newer, unrelated
        runs (F6). This concrete method is intentionally not part of the frozen
        Store protocol; the API casts to it only when a project filter is given.
        """
        clause, parameters = _where(filters, _RUN_COLUMNS)
        connector = " AND " if clause else " WHERE "
        sql = (
            f"SELECT * FROM runs{clause}{connector}"
            "task_id IN (SELECT id FROM tasks WHERE project_id = ?) "
            "ORDER BY queued_at DESC, id DESC LIMIT ?"
        )
        parameters.extend([project_id, limit])
        return _rows(self._connection.execute(sql, parameters).fetchall())

    @_writer
    def request_cancel(self, run_id: str) -> bool:
        return (
            self._write_count(
                "UPDATE runs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                (utc_now_iso(), run_id),
            )
            > 0
        )

    def requeue_paused_runs(self) -> list[str]:
        now = utc_now_iso()

        def body(connection: sqlite3.Connection) -> list[str]:
            pause = connection.execute("SELECT paused FROM pause WHERE id = 1").fetchone()
            if pause is None or bool(pause["paused"]):
                return []
            rows = connection.execute(
                "SELECT id FROM runs WHERE state = 'paused' ORDER BY queued_at ASC, id ASC"
            ).fetchall()
            run_ids = [str(row["id"]) for row in rows]
            connection.execute(
                "UPDATE runs SET state = 'queued', worker_id = NULL, updated_at = ? "
                "WHERE state = 'paused'",
                (now,),
            )
            return run_ids

        return self._execute_write_txn(body, op="sqlite_store.requeue_paused_runs")

    # --- steps ---
    def upsert_step(
        self, run_id: str, seq: int, fields: dict[str, Any], expect_worker: str | None = None
    ) -> bool:
        values = dict(_checked_fields(fields, _STEP_COLUMNS))

        def body(connection: sqlite3.Connection) -> bool:
            run_sql = "SELECT 1 FROM runs WHERE id = ?"
            run_parameters: list[Any] = [run_id]
            if expect_worker is not None:
                run_sql += " AND worker_id = ?"
                run_parameters.append(expect_worker)
            if connection.execute(run_sql, run_parameters).fetchone() is None:
                return False
            existing = connection.execute(
                "SELECT 1 FROM steps WHERE run_id = ? AND seq = ?", (run_id, seq)
            ).fetchone()
            if existing is None:
                insert_values = {"run_id": run_id, "seq": seq, **values}
                sql, parameters = _insert_sql("steps", insert_values)
                connection.execute(sql, parameters)
            elif values:
                assignments = ", ".join(f"{column} = ?" for column in values)
                connection.execute(
                    f"UPDATE steps SET {assignments} WHERE run_id = ? AND seq = ?",
                    (*values.values(), run_id, seq),
                )
            return True

        return self._execute_write_txn(body, op="sqlite_store.upsert_step")

    @_reader
    def get_steps(self, run_id: str) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM steps WHERE run_id = ? ORDER BY seq ASC", (run_id,)
            ).fetchall()
        )

    @_reader
    def get_step_counts(self, run_ids: list[str]) -> dict[str, tuple[int, int]]:
        unique = list(dict.fromkeys(run_ids))
        if not unique:
            return {}
        # Bounded by live board size; SQLite ≥3.32 var limit 32766; callers degrade gracefully.
        placeholders = ",".join("?" for _ in unique)
        rows = self._connection.execute(
            "SELECT run_id, "
            "SUM(CASE WHEN status IN ('completed', 'skipped') THEN 1 ELSE 0 END) AS done, "
            "COUNT(*) AS total FROM steps "
            f"WHERE run_id IN ({placeholders}) GROUP BY run_id",
            unique,
        ).fetchall()
        return {str(row["run_id"]): (int(row["done"] or 0), int(row["total"] or 0)) for row in rows}

    # --- idempotency receipts ---
    @_writer
    def idem_insert(self, key: str, run_id: str, step_name: str) -> bool:
        return (
            self._write_count(
                "INSERT OR IGNORE INTO idempotency "
                "(key, run_id, step_name, created_at) VALUES (?, ?, ?, ?)",
                (key, run_id, step_name, utc_now_iso()),
            )
            > 0
        )

    @_reader
    def idem_get(self, key: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute("SELECT * FROM idempotency WHERE key = ?", (key,)).fetchone()
        )

    @_writer
    def idem_complete(self, key: str, result_json: str) -> None:
        self._write(
            "UPDATE idempotency SET result_json = ?, completed_at = ? WHERE key = ?",
            (result_json, utc_now_iso(), key),
        )

    @_writer
    def idem_release(self, key: str) -> bool:
        """Drop an UNCOMPLETED claim for *key*. Returns whether a row was removed.

        The narrow inverse of :meth:`idem_insert`, for the one case where a
        claim must not survive: the effect's authority was never REACHED, so
        nothing happened, so no receipt should exist. Leaving the row would make
        the next attempt fail closed on an effect that provably did not run, and
        completing it as ``failed`` would spend an attempt slot on an outage —
        both of which turn a transient absence into a permanent verdict.

        ``result_json IS NULL`` is in the statement, not in a caller's
        precondition: a COMPLETED receipt is the record of a real effect and can
        never be deleted through this method, whatever a caller believes. The
        crash-window guarantee is therefore untouched — a claim that was left
        behind by a *crash* is still there for the next attempt to fail closed
        on, because only the code path that proved non-execution ever calls this
        (``omniagentos_loops.receipts._attempt``, on ``EffectUnavailable``).
        """
        return (
            self._write_count(
                "DELETE FROM idempotency WHERE key = ? AND result_json IS NULL", (key,)
            )
            > 0
        )

    @_reader
    def idem_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return _rows(self._connection.execute(SQL_IDEM_FOR_RUN, (run_id,)).fetchall())

    # --- tasks ---
    @_writer
    def create_task(self, row: dict[str, Any]) -> None:
        values = _checked_fields(row, _TASK_COLUMNS)
        sql, parameters = _insert_sql("tasks", values)
        self._write(sql, parameters)

    @_reader
    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        )

    @_writer
    def update_task_state(self, task_id: str, target: str, expect: list[str] | None = None) -> bool:
        if expect is not None and not expect:
            return False
        sql = "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?"
        parameters: list[Any] = [target, utc_now_iso(), task_id]
        if expect is not None:
            placeholders = ", ".join("?" for _ in expect)
            sql += f" AND state IN ({placeholders})"
            parameters.extend(expect)
        return self._write_count(sql, parameters) > 0

    @_reader
    def list_tasks(self, filters: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]:
        clause, parameters = _where(filters, _TASK_COLUMNS)
        parameters.append(limit)
        return _rows(
            self._connection.execute(
                f"SELECT * FROM tasks{clause} ORDER BY created_at DESC, id DESC LIMIT ?", parameters
            ).fetchall()
        )

    @_reader
    def list_tasks_for_project(self, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """A project's tasks, newest-active-first (updated_at) bounded in SQL.

        Ordered by updated_at (list_tasks orders by created_at) so a task that
        just picked up new run activity resurfaces to the top of a project's
        live-activity feed. Like list_runs_for_project/list_approvals_for_project,
        scoped+ordered before LIMIT so an older-but-just-updated task can't be
        pushed out of a bounded window (F6). Not part of the frozen Store protocol.
        """
        return _rows(
            self._connection.execute(
                "SELECT * FROM tasks WHERE project_id = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        )

    # --- events ---
    def insert_event(
        self,
        type: str,
        actor: str,
        action: str,
        target_type: str = "",
        target_id: str = "",
        payload: dict[str, Any] | None = None,
        trace_id: str = "",
        execution_id: str = "",
    ) -> int:
        payload_json = json.dumps(payload or {}, separators=(",", ":"), sort_keys=True)
        ts = utc_now_iso()

        def body(connection: sqlite3.Connection) -> int:
            sequence = _next_event_sequence(connection, execution_id) if execution_id else None
            cursor = connection.execute(
                "INSERT INTO events "
                "(ts, type, actor, action, target_type, target_id, payload_json, "
                "trace_id, execution_id, sequence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    type,
                    actor,
                    action,
                    target_type,
                    target_id,
                    payload_json,
                    trace_id,
                    execution_id or None,
                    sequence,
                ),
            )
            event_id = cursor.lastrowid
            if event_id is None:
                raise RuntimeError("SQLite did not return an event id")
            return int(event_id)

        return self._execute_write_txn(body, op="sqlite_store.insert_event")

    @_reader
    def get_events_after(
        self, after_id: int, types: list[str] | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE id > ?"
        parameters: list[Any] = [after_id]
        if types is not None:
            if not types:
                return []
            placeholders = ", ".join("?" for _ in types)
            sql += f" AND type IN ({placeholders})"
            parameters.extend(types)
        sql += " ORDER BY id ASC LIMIT ?"
        parameters.append(limit)
        return _rows(self._connection.execute(sql, parameters).fetchall())

    @_reader
    def audit_events_for_target(
        self,
        *,
        target_type: str,
        target_id: str,
        action_prefix: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Newest audit events for one target/action namespace."""

        if limit <= 0:
            return []
        return _rows(
            self._connection.execute(
                "SELECT * FROM events WHERE type = 'audit.event' "
                "AND target_type = ? AND target_id = ? AND action LIKE ? "
                "ORDER BY id DESC LIMIT ?",
                (target_type, target_id, f"{action_prefix}%", limit),
            ).fetchall()
        )

    @_reader
    def get_events_for_run(self, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
        """Return the newest bounded set of directly targeted run events.

        This concrete query intentionally is not part of the frozen Store
        protocol. The API uses it for its SQLite-backed run-detail endpoint.
        """
        return _rows(
            self._connection.execute(
                "SELECT * FROM (SELECT * FROM events "
                "WHERE target_type = 'run' AND target_id = ? "
                "ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (run_id, limit),
            ).fetchall()
        )

    @_reader
    def get_events_for_target(
        self,
        target_type: str,
        target_id: str,
        after_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Chronological events for one ``(target_type, target_id)``, after a cursor.

        Generalizes ``get_events_for_run``'s hardcoded ``target_type='run'``
        query with an ``after_id`` cursor for polling — WP6a's swarm activity
        feed (``GET /api/swarm/{id}/activity?after=``) is the first caller, but
        the query is target-type-agnostic so a future feed (agent activity,
        category activity, ...) can reuse it instead of hand-rolling another
        target_type/target_id events query. Not part of the frozen Store
        protocol.
        """
        return _rows(
            self._connection.execute(
                "SELECT * FROM events WHERE target_type = ? AND target_id = ? "
                "AND id > ? ORDER BY id ASC LIMIT ?",
                (target_type, target_id, after_id, limit),
            ).fetchall()
        )

    @_reader
    def list_events_for_project(self, project_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Events for a project's runs AND approvals, newest-first bounded.

        Two event families carry a project's live activity: run-lifecycle events
        (run.updated / step.updated / task.updated / audit.event /
        approval.requested), always persisted with target_type='run' (see
        runner.core.Runner._event), and approval.decided, persisted with
        target_type='approval' by the API's decide-approval route. Both are
        joined back to project_id via their owning task (runs through
        runs.task_id, approvals through approvals.task_id) and merged
        newest-first before LIMIT -- same F6 scoping reasoning as
        list_runs_for_project/list_approvals_for_project. Not part of the
        frozen Store protocol.
        """
        sql = (
            "SELECT * FROM events WHERE target_type = 'run' AND target_id IN "
            "(SELECT id FROM runs WHERE task_id IN (SELECT id FROM tasks WHERE project_id = ?)) "
            "UNION ALL "
            "SELECT * FROM events WHERE target_type = 'approval' AND target_id IN "
            "(SELECT id FROM approvals WHERE task_id IN (SELECT id FROM tasks WHERE project_id = ?)) "
            "ORDER BY id DESC LIMIT ?"
        )
        return _rows(self._connection.execute(sql, (project_id, project_id, limit)).fetchall())

    @_reader
    def list_project_ids(self, limit: int = 500) -> list[str]:
        """Every project id, oldest-created-first, bounded.

        For callers that must sweep all projects (activity_tick) without a
        bespoke query. Not part of the frozen Store protocol.
        """
        rows = self._connection.execute(
            "SELECT id FROM projects ORDER BY created_at ASC, id ASC LIMIT ?", (limit,)
        ).fetchall()
        return [str(row["id"]) for row in rows]

    @_reader
    def latest_event_id(self) -> int:
        row = self._connection.execute("SELECT COALESCE(MAX(id), 0) AS id FROM events").fetchone()
        return 0 if row is None else int(row["id"])

    # --- approvals ---
    @_writer
    def create_approval(self, row: dict[str, Any]) -> None:
        values = _checked_fields(row, _APPROVAL_COLUMNS)
        sql, parameters = _insert_sql("approvals", values)
        self._write(sql, parameters)

    @_reader
    def get_approval_for(self, run_id: str, step_seq: int | None) -> dict[str, Any] | None:
        # Prefer live pending/approved over expired siblings from re-queue
        # (AUTO-APPROVE 5.2): same created_at second can make id-only ORDER
        # pick the expired row and leave the run stuck.
        return _row(self._connection.execute(SQL_GET_APPROVAL_FOR, (run_id, step_seq)).fetchone())

    @_writer
    def decide_approval(
        self, approval_id: str, state: str, decided_by: str, note: str | None = None
    ) -> bool:
        return (
            self._write_count(
                "UPDATE approvals SET state = ?, decided_by = ?, decision_note = ?, decided_at = ? "
                "WHERE id = ? AND state = 'pending'",
                (state, decided_by, note, utc_now_iso(), approval_id),
            )
            > 0
        )

    @_writer
    def void_pending_approvals(self, run_id: str, note: str) -> int:
        return self._write_count(
            "UPDATE approvals SET state = 'expired', decision_note = ?, decided_at = ? "
            "WHERE run_id = ? AND state = 'pending'",
            (note, utc_now_iso(), run_id),
        )

    @_reader
    def list_approvals(self, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if state is None:
            return _rows(
                self._connection.execute(
                    "SELECT * FROM approvals ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
                ).fetchall()
            )
        return _rows(
            self._connection.execute(
                "SELECT * FROM approvals WHERE state = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (state, limit),
            ).fetchall()
        )

    @_reader
    def list_approvals_for_project(
        self, project_id: str, state: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Project approvals plus session approvals not owned by any project task.

        Like :meth:`list_runs_for_project`, scoping is applied in SQL before
        ORDER BY / LIMIT so a project's older approvals survive a global window
        of newer ones (F6). Not part of the frozen Store protocol.
        """
        parameters: list[Any] = []
        sql = "SELECT * FROM approvals WHERE "
        if state is not None:
            sql += "state = ? AND "
            parameters.append(state)
        sql += (
            "(task_id IN (SELECT id FROM tasks WHERE project_id = ?) "
            "OR (session_id IS NOT NULL AND (task_id IS NULL OR task_id = '' "
            "OR task_id NOT IN (SELECT id FROM tasks WHERE project_id IS NOT NULL)))) "
            "ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        parameters.extend([project_id, limit])
        return _rows(self._connection.execute(sql, parameters).fetchall())

    # --- pause / heartbeats ---
    @_reader
    def get_pause(self) -> dict[str, Any]:
        row = _row(self._connection.execute("SELECT * FROM pause WHERE id = 1").fetchone())
        if row is None:
            raise RuntimeError("pause row is missing")
        return row

    @_writer
    def set_pause(self, paused: bool, reason: str = "") -> dict[str, Any]:
        self._write(
            "UPDATE pause SET paused = ?, reason = ?, updated_at = ? WHERE id = 1",
            (int(paused), reason, utc_now_iso()),
        )
        return self.get_pause()

    @_writer
    def upsert_heartbeat(self, worker_id: str, pid: int, current_run_id: str | None) -> None:
        now = utc_now_iso()
        self._write(
            "INSERT INTO heartbeats "
            "(worker_id, pid, started_at, last_beat_at, current_run_id) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(worker_id) DO UPDATE SET "
            "pid = excluded.pid, last_beat_at = excluded.last_beat_at, "
            "current_run_id = excluded.current_run_id",
            (worker_id, pid, now, now, current_run_id),
        )

    @_reader
    def get_heartbeats(self) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute("SELECT * FROM heartbeats ORDER BY worker_id ASC").fetchall()
        )

    # --- provider-call ledger ---
    def record_provider_call(self, observation: CostObservation | dict[str, Any]) -> dict[str, Any]:
        """Persist one call exactly once; identical replays return the same row."""

        return self._execute_write_txn(
            lambda connection: _record_provider_call_row(connection, observation),
            op="sqlite_store.record_provider_call",
        )

    def reserve_provider_spend(
        self,
        *,
        provider: str,
        utc_day: str,
        cap_usd_nanos: int,
        proposed: CostObservation | dict[str, Any],
        refused: CostObservation | dict[str, Any],
        override: bool = False,
    ) -> dict[str, Any]:
        """Atomically compare daily spend and record the allowed/refused attempt.

        ``BEGIN IMMEDIATE`` is part of the money boundary: two workers cannot
        both observe the same remaining headroom and reserve it.  An override
        changes only the decision; the provider/day projection remains visible
        and the caller must separately write its override audit marker.
        """

        if cap_usd_nanos < 0:
            raise ValueError("cap_usd_nanos must be non-negative")

        def body(connection: sqlite3.Connection) -> dict[str, Any]:
            prior = _provider_spend_nanos(
                connection,
                provider=provider,
                utc_day=utc_day,
            )
            proposed_payload = _provider_call_payload(proposed)
            ceiling = int(proposed_payload.get("cost_upper_bound_usd_nanos") or 0)
            projected = prior + ceiling
            allowed = projected <= cap_usd_nanos or override
            chosen = proposed if allowed else refused
            row = _record_provider_call_row(connection, chosen)
            return {
                "allowed": allowed,
                "override": bool(override and projected > cap_usd_nanos),
                "prior_usd_nanos": prior,
                "proposed_usd_nanos": ceiling,
                "projected_usd_nanos": projected,
                "cap_usd_nanos": cap_usd_nanos,
                "row": row,
            }

        return self._execute_write_txn(
            body,
            op="sqlite_store.reserve_provider_spend",
        )

    def deliver_provider_alert_once(
        self,
        *,
        provider: str,
        utc_day: str,
        marker: CostObservation | dict[str, Any],
        deliver: Callable[[], bool],
    ) -> bool:
        """Claim quickly, deliver outside SQLite, then confirm on success.

        Active claims suppress concurrent duplicate sends. Failed deliveries
        release their claim immediately; a process killed between claim and
        confirm leaves a claim that expires after 60 seconds.
        """

        execution_id = f"spend-alert:{provider}:{utc_day}"
        raw_marker = (
            marker.model_dump(mode="json")
            if isinstance(marker, CostObservation)
            else dict(marker)
        )
        created_at = str(raw_marker.get("created_at") or utc_now_iso())
        try:
            claim_now = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"provider alert marker has invalid created_at {created_at!r}") from exc
        if claim_now.tzinfo is None:
            claim_now = claim_now.replace(tzinfo=UTC)
        claim_cutoff = (claim_now.astimezone(UTC) - timedelta(
            seconds=PROVIDER_ALERT_CLAIM_TTL_SECONDS
        )).strftime("%Y-%m-%dT%H:%M:%SZ")
        claim_id = f"{execution_id}:claim:{new_id('call')}"
        claim_marker = dict(raw_marker)
        claim_marker.update(
            {
                "call_id": claim_id,
                "provider_outcome": "soft_threshold_alert_claimed",
                "cost_source": "spend-cap-alert:claimed",
                "settled_at": None,
            }
        )

        def claim(connection: sqlite3.Connection) -> bool:
            existing = connection.execute(
                "SELECT 1 FROM provider_call_usage "
                "WHERE execution_id = ? AND cost_source = 'spend-cap-alert:delivered' LIMIT 1",
                (execution_id,),
            ).fetchone()
            if existing is not None:
                return False
            active_claim = connection.execute(
                "SELECT 1 FROM provider_call_usage "
                "WHERE execution_id = ? AND cost_source = 'spend-cap-alert:claimed' "
                "AND created_at > ? LIMIT 1",
                (execution_id, claim_cutoff),
            ).fetchone()
            if active_claim is not None:
                return False
            _record_provider_call_row(connection, claim_marker)
            return True

        claimed = self._execute_write_txn(
            claim,
            op="sqlite_store.claim_provider_alert",
        )
        if not claimed:
            return False

        # Deliberately outside every BEGIN IMMEDIATE transaction.
        delivered = bool(deliver())

        def confirm(connection: sqlite3.Connection) -> bool:
            existing_claim = _get_provider_call_row(connection, claim_id)
            if existing_claim is None:
                raise KeyError(f"provider alert claim disappeared: {claim_id}")
            if delivered:
                existing_delivery = connection.execute(
                    "SELECT 1 FROM provider_call_usage "
                    "WHERE execution_id = ? "
                    "AND cost_source = 'spend-cap-alert:delivered' LIMIT 1",
                    (execution_id,),
                ).fetchone()
                if existing_delivery is None:
                    _record_provider_call_row(connection, marker)
                claim_outcome = "soft_threshold_alert_claim_confirmed"
                claim_source = "spend-cap-alert:claim-confirmed"
            else:
                claim_outcome = "soft_threshold_alert_delivery_failed"
                claim_source = "spend-cap-alert:claim-failed"
            _settle_provider_call_row(
                connection,
                claim_id,
                {
                    "provider_outcome": claim_outcome,
                    "cost_source": claim_source,
                },
            )
            return delivered

        return self._execute_write_txn(
            confirm,
            op="sqlite_store.confirm_provider_alert",
        )

    def settle_provider_call(
        self,
        call_id: str,
        settlement: CostObservation | dict[str, Any],
    ) -> dict[str, Any]:
        """Settle an initiated call exactly once, rejecting conflicting replay."""

        return self._execute_write_txn(
            lambda connection: _settle_provider_call_row(connection, call_id, settlement),
            op="sqlite_store.settle_provider_call",
        )

    def sweep_stale_provider_reservations(self, *, before: str) -> int:
        """Settle abandoned preflight reservations to bounded indeterminate rows."""

        def body(connection: sqlite3.Connection) -> int:
            rows = connection.execute(
                "SELECT call_id FROM provider_call_usage "
                "WHERE provider_outcome = 'spend_reserved' "
                "AND request_state = 'not_sent' AND settled_at IS NULL "
                "AND created_at < ? ORDER BY created_at, id",
                (before,),
            ).fetchall()
            for row in rows:
                _settle_provider_call_row(
                    connection,
                    str(row["call_id"]),
                    {
                        "request_state": "indeterminate",
                        "provider_outcome": "stale_reservation_swept",
                        "cost_source": "spend-cap-upper-bound:stale-reservation-sweep",
                    },
                )
            return len(rows)

        return self._execute_write_txn(
            body,
            op="sqlite_store.sweep_stale_provider_reservations",
        )

    @_reader
    def provider_spend_day(
        self,
        *,
        provider: str | None = None,
        utc_day: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read the query-layer ``spend_day`` projection."""

        return _provider_spend_day_rows(
            self._connection,
            provider=provider,
            utc_day=utc_day,
        )

    @_reader
    def get_provider_call(self, call_id: str) -> dict[str, Any] | None:
        return _get_provider_call_row(self._connection, call_id)

    @_reader
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

    @_reader
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

    # --- budgets / artifacts / disciplines ---
    @_reader
    def get_budget(self, budget_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute("SELECT * FROM budgets WHERE id = ?", (budget_id,)).fetchone()
        )

    @_writer
    def upsert_budget_usage(
        self, budget_id: str, wall_ms: int, tokens: int, cost_usd: float
    ) -> None:
        if budget_id == "global":
            scope_type, scope_id = "global", ""
        elif ":" in budget_id:
            scope_type, scope_id = budget_id.split(":", 1)
        else:
            scope_type, scope_id = budget_id, ""
        self._write(
            "INSERT INTO budgets "
            "(id, scope_type, scope_id, used_wall_ms, used_tokens, used_cost_usd, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "used_wall_ms = budgets.used_wall_ms + excluded.used_wall_ms, "
            "used_tokens = budgets.used_tokens + excluded.used_tokens, "
            "used_cost_usd = budgets.used_cost_usd + excluded.used_cost_usd, "
            "updated_at = excluded.updated_at",
            (budget_id, scope_type, scope_id, wall_ms, tokens, cost_usd, utc_now_iso()),
        )

    @_reader
    def list_budgets(self) -> list[dict[str, Any]]:
        return _rows(self._connection.execute("SELECT * FROM budgets ORDER BY id ASC").fetchall())

    @_writer
    def add_artifact(self, row: dict[str, Any]) -> None:
        values = _checked_fields(row, _ARTIFACT_COLUMNS)
        sql, parameters = _insert_sql("artifacts", values)
        self._write(sql, parameters)

    @_reader
    def get_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at ASC, id ASC",
                (run_id,),
            ).fetchall()
        )

    @_reader
    def list_disciplines(self) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM disciplines ORDER BY created_at ASC, id ASC"
            ).fetchall()
        )

    @_writer
    def create_discipline(self, row: dict[str, Any]) -> bool:
        values = _checked_fields(row, _DISCIPLINE_COLUMNS)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        return (
            self._write_count(
                f"INSERT OR IGNORE INTO disciplines ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            > 0
        )

    # ``_write`` / ``_write_count`` are the one-statement raw-SQL escape hatch and
    # are called from OUTSIDE this class (ProvisionStore, LabStore, tests) without
    # any surrounding `with store._lock:`. They each contain a whole transaction
    # under M-33 busy retry; the writer lock is taken inside each attempt.
    # ``_lock`` is an RLock, so a ``@_writer`` method that already holds it
    # re-enters for free (backoff then sleeps while still holding the outer lock).
    # Prefer ``_execute_write_txn`` for multi-statement writers so lock is released
    # between retries. Hand-rolled multi-statement ``_begin()`` still has to be
    # locked by its caller.
    def _write(self, sql: str, parameters: Iterable[Any]) -> None:
        bound = tuple(parameters)

        def body(connection: sqlite3.Connection) -> None:
            connection.execute(sql, bound)

        self._execute_write_txn(body, op="sqlite_store._write")

    def _write_count(self, sql: str, parameters: Iterable[Any]) -> int:
        bound = tuple(parameters)

        def body(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(sql, bound)
            # rowcount is read BEFORE commit and is per-cursor, hence per
            # connection: thread affinity makes it strictly more reliable than
            # it was on one shared handle, never less.
            return int(cursor.rowcount)

        return self._execute_write_txn(body, op="sqlite_store._write_count")
