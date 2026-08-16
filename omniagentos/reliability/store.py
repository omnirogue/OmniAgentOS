"""Concrete SQLite implementation of ReliabilityStore protocol (W1).

Enforces all durability contracts: CAS transitions with atomic log append, occurrence_key
idempotency, lease-based mutual exclusion, SQLITE_BUSY retry, cursor semantics, hash-chained
append-only log. See §5b for the guarantees.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.busy import execute_write_transaction
from omniagentos.db.store import SqliteStore
from omniagentos.reliability.contracts import (
    Agent,
    AgentRequest,
    AutonomySetting,
    Improvement,
    ImprovementVote,
    LeaseConflict,
    OrgUnit,
    ReliabilityAudit,
    ReliabilityEvent,
    Scorecard,
    TransitionConflict,
)
from omniagentos.reliability.taxonomy import (
    AuditStatus,
    AutonomyMode,
    EventStatus,
    ImprovementStatus,
)

# Constants
#
# The bounded-retry attempt/backoff constants that used to live here (M-33
# HANDOFF adoption) are retired: ``_execute_with_retry`` now routes every
# reliability write through ``omniagentos.db.busy.execute_write_transaction``,
# which owns its own policy (``DEFAULT_BUSY_RETRY_POLICY``) and the
# process-wide ``BUSY_RETRY_METRICS`` counters. See HANDOFF/L14-M33.

# Hash chain genesis value
_GENESIS_HASH = "0" * 64


# --- Request attribution (U-E7): canonical identity validation
#
# Migration 105 adds FOUR columns to agent_requests — from_agent_id, from_lane,
# session_id, run_id. Only from_agent_id is the identity; the other three are the
# context that makes it auditable.
#
# LEGACY ROWS: anything written before 105 carries requested_by='owner', which is
# not a canonical spelling and is NOT backfilled. Those values are legacy display
# attribution and must not be read as a principal. Normalising them (to
# 'human:owner', or to 'system' where the row was machine-created) needs a
# per-row truth decision and belongs in a follow-up migration; until then, a
# requested_by that fails the grammar below identifies a pre-U-E7 row.
#
# This note lives here rather than in 105_request_attribution.sql because
# migrations are checksum-verified after apply: editing a landed migration file,
# even its comments, fails every database that already ran it.


_logger = logging.getLogger(__name__)

# Canonical identity spellings: lane:*, loop:*, job:*, human:*, system
# NOT agent:* (must be rejected at write path)
#: The canonical identity grammar, anchored. A prefix test admitted
#: ``system_impostor`` (startswith "system") and a bare ``lane:`` with no
#: subject at all, both of which are attributions nobody can act on.
_CANONICAL_IDENTITY_RE = re.compile(r"system|(?:lane|loop|job|human):[A-Za-z0-9][A-Za-z0-9._-]*")


def _is_canonical_identity(identity: str) -> bool:
    """Validate canonical spelling for from_agent_id.

    Canonical spellings:
    - lane:* (lane:runner.step, lane:swarm.planner, lane:swarm.worker.<formation>, etc.)
    - loop:* (loop:<instance_id>)
    - job:* (job:<catalog-key>)
    - human:* (human:<operator>)
    - system (for missing auth or internal system events)

    The subject after the prefix is required and must start alphanumeric, so
    ``lane:`` and ``human:-`` are refused alongside the non-canonical ``agent:*``.
    """
    if not identity:
        return False
    return _CANONICAL_IDENTITY_RE.fullmatch(identity) is not None


def _row_value(row: sqlite3.Row, column: str, default: Any = None) -> Any:
    """Read an optional column from a ``sqlite3.Row``.

    ``sqlite3.Row`` is a sequence with mapping-style subscripting; it has NO
    ``.get()``. Calling ``row.get(...)`` raised ``AttributeError`` on every real
    read — the fault was invisible only because the request tests re-implemented
    the store. Columns added by a migration can still be absent when a caller
    opens a database that has not been migrated up, so the membership test stays.
    """
    return row[column] if column in row.keys() else default


def _validate_from_agent_id(from_agent_id: str) -> None:
    """Validate from_agent_id spelling before insert.

    Raises ValueError if not canonical.
    """
    if not _is_canonical_identity(from_agent_id):
        raise ValueError(
            f"Invalid from_agent_id: {from_agent_id!r}. "
            f"Must use canonical spelling: lane:*, loop:*, job:*, human:*, or system. "
            f"agent:* spelling is not allowed."
        )


def _normalize_requested_by(from_agent_id: str) -> str:
    """Display attribution: return from_agent_id as the canonical spelling.

    This becomes the requested_by value shown in responses.
    """
    return from_agent_id


def _canonical_json(obj: Any) -> str:
    """Canonical JSON for hash chaining: sorted keys, no spaces."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def _hash_log_row(prev_hash: str, row: dict[str, Any]) -> str:
    """SHA256 of (prev_hash + canonical JSON of row)."""
    data = prev_hash + _canonical_json(row)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@dataclass
class _LogRowParams:
    """Internal: parameters for a reliability_log row."""

    entity_type: str
    entity_id: str
    to_status: str
    actor: str
    ts: str
    detail_json: dict[str, Any] | None = None
    from_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for canonical JSON."""
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "actor": self.actor,
            "detail_json": self.detail_json or {},
            "ts": self.ts,
        }


class SqliteReliabilityStore(SqliteStore):
    """SQLite implementation of ReliabilityStore protocol.

    Inherits from SqliteStore for connection pool + pragma setup. Adds reliability-specific
    methods with proper transaction handling, hash chaining, and durability contracts.
    """

    def __init__(self, db_path: str) -> None:
        """Initialize with DB path. Auto-migrates on first open."""
        super().__init__(db_path)
        self._log_lock = RLock()  # Protect log chain reads/writes

    # --- Helpers

    def _get_prev_log_hash(self, connection: sqlite3.Connection) -> str:
        """Fetch the most recent log row's hash, or genesis value if empty."""
        row = connection.execute(
            "SELECT hash FROM reliability_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["hash"] if row else _GENESIS_HASH

    def _log_row_to_db(
        self,
        connection: sqlite3.Connection,
        params: _LogRowParams,
    ) -> str:
        """Append immutable row to reliability_log and return its hash (§6, m3)."""
        with self._log_lock:
            prev_hash = self._get_prev_log_hash(connection)
            row_dict = params.to_dict()
            hash_val = _hash_log_row(prev_hash, row_dict)

            connection.execute(
                """
                INSERT INTO reliability_log
                  (entity_type, entity_id, from_status, to_status, actor, detail_json, ts, prev_hash, hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    params.entity_type,
                    params.entity_id,
                    params.from_status,
                    params.to_status,
                    params.actor,
                    json.dumps(params.detail_json or {}),
                    params.ts,
                    prev_hash,
                    hash_val,
                ),
            )
        return hash_val

    def _execute_with_retry(self, func: Callable[[sqlite3.Connection], Any]) -> Any:
        """Execute a function on a connection with busy-retry.

        HANDOFF/L14-M33 adoption: every reliability write now runs through the
        shared ``omniagentos.db.busy.execute_write_transaction`` seam instead of
        a hand-rolled BEGIN/COMMIT/ROLLBACK + backoff loop. Same shape as before
        (rollback residual, ``BEGIN IMMEDIATE``, run ``func``, commit, rollback +
        re-raise on any error) but retries/backoff/jitter/observability now come
        from the shared primitive (``BUSY_RETRY_METRICS``), so contention here is
        counted alongside every other adopter instead of in a private counter.

        Runs under the base store's writer lock -- the same lock every ``@_writer``
        method takes -- so a reliability write and a board write never open two
        transactions against the same database at once. The lock is taken INSIDE
        the retried attempt (via ``execute_write_transaction``'s ``lock=``) so the
        busy-backoff sleeps with the lock released. It is an RLock, so a caller
        that already holds it re-enters harmlessly.
        """
        return execute_write_transaction(
            self._connection,
            func,
            lock=self._lock,
            op=f"reliability_store.{func.__name__}",
        )

    # --- Event lifecycle

    def insert_reliability_event(
        self,
        failure_class: str,
        severity: str,
        signature: str,
        occurrence_key: str,
        source: str,
        ref_type: str | None = None,
        ref_id: str | None = None,
        evidence_json: dict[str, Any] | None = None,
    ) -> str:
        """Insert into ``reliability_events``; ON CONFLICT IGNORE for idempotency (§5b.5).

        Deliberately not named ``insert_event``: the base ``SqliteStore`` method
        writes the general SSE ``events`` table with a different signature and
        return type. Callers must use this name for reliability rows.
        """

        def _insert(conn: sqlite3.Connection) -> str:
            event_id = new_id("evt")
            now = utc_now_iso()
            conn.execute(
                """
                INSERT OR IGNORE INTO reliability_events
                  (id, failure_class, severity, signature, occurrence_key, source, ref_type, ref_id,
                   evidence_json, status, detected_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    failure_class,
                    severity,
                    signature,
                    occurrence_key,
                    source,
                    ref_type,
                    ref_id,
                    json.dumps(evidence_json or {}),
                    EventStatus.OPEN.value,
                    now,
                    now,
                ),
            )
            return event_id

        return self._execute_with_retry(lambda conn: _insert(conn))

    def get_event(self, event_id: str) -> ReliabilityEvent | None:
        """Fetch event by id."""

        def _get(conn: sqlite3.Connection) -> ReliabilityEvent | None:
            row = conn.execute(
                "SELECT * FROM reliability_events WHERE id = ?", (event_id,)
            ).fetchone()
            if not row:
                return None
            return ReliabilityEvent(
                id=row["id"],
                failure_class=row["failure_class"],
                severity=row["severity"],
                signature=row["signature"],
                occurrence_key=row["occurrence_key"],
                source=row["source"],
                ref_type=row["ref_type"],
                ref_id=row["ref_id"],
                evidence_json=json.loads(row["evidence_json"] or "{}"),
                status=row["status"],
                recovery_json=json.loads(row["recovery_json"] or "{}"),
                improvement_id=row["improvement_id"],
                audit_id=row["audit_id"],
                detected_at=row["detected_at"],
                updated_at=row["updated_at"],
                alerted_at=row["alerted_at"],
            )

        return self._execute_with_retry(_get)

    def list_events(
        self,
        status: str | None = None,
        severity: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReliabilityEvent]:
        """List events with optional filters."""

        def _list(conn: sqlite3.Connection) -> list[ReliabilityEvent]:
            query = "SELECT * FROM reliability_events WHERE 1=1"
            params: list[Any] = []
            if status is not None:
                query += " AND status = ?"
                params.append(status)
            if severity is not None:
                query += " AND severity = ?"
                params.append(severity)
            query += " ORDER BY detected_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(query, params).fetchall()
            events = []
            for row in rows:
                events.append(
                    ReliabilityEvent(
                        id=row["id"],
                        failure_class=row["failure_class"],
                        severity=row["severity"],
                        signature=row["signature"],
                        occurrence_key=row["occurrence_key"],
                        source=row["source"],
                        ref_type=row["ref_type"],
                        ref_id=row["ref_id"],
                        evidence_json=json.loads(row["evidence_json"] or "{}"),
                        status=row["status"],
                        recovery_json=json.loads(row["recovery_json"] or "{}"),
                        improvement_id=row["improvement_id"],
                        audit_id=row["audit_id"],
                        detected_at=row["detected_at"],
                        updated_at=row["updated_at"],
                        alerted_at=row["alerted_at"],
                    )
                )
            return events

        return self._execute_with_retry(_list)

    def count_open_events_by_severity(self) -> dict[str, int]:
        """Count every open event by severity with one aggregate query."""

        def _count(conn: sqlite3.Connection) -> dict[str, int]:
            rows = conn.execute(
                """
                SELECT severity, COUNT(*) AS total
                FROM reliability_events
                WHERE status = ?
                GROUP BY severity
                """,
                (EventStatus.OPEN.value,),
            ).fetchall()
            counts = {"info": 0, "warning": 0, "critical": 0}
            for row in rows:
                severity = str(row["severity"])
                if severity in counts:
                    counts[severity] = int(row["total"])
            return counts

        return self._execute_with_retry(_count)

    def list_events_awaiting_alert(
        self,
        severity: str,
        status: str = EventStatus.OPEN.value,
        realert_before: str | None = None,
        limit: int = 200,
    ) -> list[ReliabilityEvent]:
        """Events that still owe the operator an alert (never alerted, or stale).

        The alert-once seam. ``_critical_alerts`` used to re-notify every open
        critical event on every 600 s watch cycle; recovery only allow-lists
        rate_limit/timeout, so a session_error event stayed 'open' forever and
        re-fired 144x/day. Selecting on ``alerted_at`` bounds that to one alert
        per event, plus one per ``realert_before`` window when re-alerting is on.
        """

        def _list(conn: sqlite3.Connection) -> list[ReliabilityEvent]:
            query = (
                "SELECT * FROM reliability_events "
                "WHERE severity = ? AND status = ? "
                "  AND (alerted_at IS NULL OR (? IS NOT NULL AND alerted_at < ?)) "
                "ORDER BY detected_at ASC LIMIT ?"
            )
            rows = conn.execute(
                query, (severity, status, realert_before, realert_before, limit)
            ).fetchall()
            return [
                ReliabilityEvent(
                    id=row["id"],
                    failure_class=row["failure_class"],
                    severity=row["severity"],
                    signature=row["signature"],
                    occurrence_key=row["occurrence_key"],
                    source=row["source"],
                    ref_type=row["ref_type"],
                    ref_id=row["ref_id"],
                    evidence_json=json.loads(row["evidence_json"] or "{}"),
                    status=row["status"],
                    recovery_json=json.loads(row["recovery_json"] or "{}"),
                    improvement_id=row["improvement_id"],
                    audit_id=row["audit_id"],
                    detected_at=row["detected_at"],
                    updated_at=row["updated_at"],
                    alerted_at=row["alerted_at"],
                )
                for row in rows
            ]

        return self._execute_with_retry(_list)

    def mark_event_alerted(self, event_id: str, alerted_at: str | None = None) -> bool:
        """Stamp ``alerted_at`` so the event is not re-alerted next cycle.

        Deliberately does NOT touch ``updated_at``: alerting is a notification
        side-channel, not a state transition, and the stale-event reclaim reads
        ``updated_at`` to decide what has genuinely stopped progressing.
        """

        def _mark(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE reliability_events SET alerted_at = ? WHERE id = ?",
                (alerted_at or utc_now_iso(), event_id),
            )
            return cursor.rowcount > 0

        return self._execute_with_retry(_mark)

    def claim_recovery(self, event_id: str) -> bool:
        """CAS: open → recovering (§5b.5). Returns True if caller won."""

        def _claim(conn: sqlite3.Connection) -> bool:
            # Check current status
            row = conn.execute(
                "SELECT status FROM reliability_events WHERE id = ?", (event_id,)
            ).fetchone()
            if not row or row["status"] != EventStatus.OPEN.value:
                # Rollback since we're not making changes
                conn.rollback()
                self._connection.execute("BEGIN IMMEDIATE")
                return False

            # Update status
            conn.execute(
                "UPDATE reliability_events SET status = ?, updated_at = ? WHERE id = ?",
                (EventStatus.RECOVERING.value, utc_now_iso(), event_id),
            )
            return True

        return self._execute_with_retry(_claim)

    def close_event(
        self,
        event_id: str,
        *,
        actor: str = "system",
        reason: str = "resolved",
        expected: str | None = None,
    ) -> bool:
        """Close an open/recovering event (F2 — events must leave ``open``).

        Terminal status is ``resolved``. Returns False if missing or CAS miss.
        """
        return self.set_event_status(
            event_id,
            EventStatus.RESOLVED.value,
            actor=actor,
            detail={"reason": reason, "source": "close_event"},
            expected=expected,
        )

    def auto_close_stale_events(
        self,
        *,
        quiet_hours: float = 24.0,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Auto-close open events whose failure class has been quiet for N hours (F2).

        An event is closed when no newer open event shares its ``signature`` (or
        failure_class+ref) within the quiet window, and the event itself is older
        than ``quiet_hours``.
        """
        from datetime import UTC, datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(hours=quiet_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def _sweep(conn: sqlite3.Connection) -> dict[str, Any]:
            rows = conn.execute(
                "SELECT id, signature, failure_class, detected_at, updated_at "
                "FROM reliability_events WHERE status = ? "
                "AND COALESCE(updated_at, detected_at) < ? "
                "ORDER BY detected_at ASC LIMIT ?",
                (EventStatus.OPEN.value, cutoff, limit),
            ).fetchall()
            closed = 0
            for row in rows:
                # Skip if a newer open event with the same signature still exists.
                newer = conn.execute(
                    "SELECT 1 FROM reliability_events "
                    "WHERE status = ? AND signature = ? AND id != ? "
                    "AND COALESCE(updated_at, detected_at) >= ? LIMIT 1",
                    (
                        EventStatus.OPEN.value,
                        row["signature"],
                        row["id"],
                        cutoff,
                    ),
                ).fetchone()
                if newer:
                    continue
                ok = self.set_event_status(
                    row["id"],
                    EventStatus.RESOLVED.value,
                    actor="system:auto_close",
                    detail={
                        "reason": "quiet_window",
                        "quiet_hours": quiet_hours,
                        "source": "auto_close_stale_events",
                    },
                    expected=EventStatus.OPEN.value,
                )
                if ok:
                    closed += 1
            return {"closed": closed, "candidates": len(rows), "cutoff": cutoff}

        return self._execute_with_retry(_sweep)

    def set_event_status(
        self,
        event_id: str,
        status: str,
        actor: str = "system",
        detail: dict[str, Any] | None = None,
        expected: str | None = None,
    ) -> bool:
        """CAS event status transition + reliability_log row (codex #15).

        Used by the ignore route and recovery terminal transitions. Returns False
        when the event is missing or ``expected`` no longer matches.
        """

        def _set(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT status FROM reliability_events WHERE id = ?", (event_id,)
            ).fetchone()
            if not row or (expected is not None and row["status"] != expected):
                return False
            now = utc_now_iso()
            conn.execute(
                "UPDATE reliability_events SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, event_id),
            )
            self._log_row_to_db(
                conn,
                _LogRowParams(
                    entity_type="event",
                    entity_id=event_id,
                    from_status=row["status"],
                    to_status=status,
                    actor=actor,
                    detail_json=detail or {},
                    ts=now,
                ),
            )
            return True

        return self._execute_with_retry(_set)

    def update_event_recovery(
        self,
        event_id: str,
        recovery_json: dict[str, Any],
        status: str | None = None,
        actor: str = "recovery",
    ) -> bool:
        """Persist recovery actions onto an event (merge), optionally moving status.

        Recovery that acted must leave a durable record (codex #4): the merged
        ``recovery_json`` survives, and any status move appends a log row.
        """

        def _upd(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT status, recovery_json FROM reliability_events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if not row:
                return False
            now = utc_now_iso()
            try:
                merged = json.loads(row["recovery_json"] or "{}")
            except ValueError:
                merged = {}
            merged.update(recovery_json or {})
            new_status = status or row["status"]
            conn.execute(
                "UPDATE reliability_events SET recovery_json = ?, status = ?, updated_at = ? WHERE id = ?",
                (json.dumps(merged), new_status, now, event_id),
            )
            if new_status != row["status"]:
                self._log_row_to_db(
                    conn,
                    _LogRowParams(
                        entity_type="event",
                        entity_id=event_id,
                        from_status=row["status"],
                        to_status=new_status,
                        actor=actor,
                        detail_json={"recovery": True},
                        ts=now,
                    ),
                )
            return True

        return self._execute_with_retry(_upd)

    # --- Improvement lifecycle

    def create_improvement(
        self,
        origin: str,
        kind: str,
        title: str,
        summary: str = "",
        root_cause: str = "",
        proposal_json: dict[str, Any] | None = None,
        created_by: str = "system",
    ) -> str:
        """Create improvement in proposed status."""

        def _create(conn: sqlite3.Connection) -> str:
            imp_id = new_id("imp")
            now = utc_now_iso()
            conn.execute(
                """
                INSERT INTO improvements
                  (id, origin, kind, title, summary, root_cause, proposal_json, status, version,
                   created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    imp_id,
                    origin,
                    kind,
                    title,
                    summary,
                    root_cause,
                    json.dumps(proposal_json or {}),
                    ImprovementStatus.PROPOSED.value,
                    0,
                    created_by,
                    now,
                    now,
                ),
            )
            return imp_id

        return self._execute_with_retry(_create)

    def get_improvement(self, imp_id: str) -> Improvement | None:
        """Fetch improvement by id."""

        def _get(conn: sqlite3.Connection) -> Improvement | None:
            row = conn.execute("SELECT * FROM improvements WHERE id = ?", (imp_id,)).fetchone()
            if not row:
                return None
            return Improvement(
                id=row["id"],
                origin=row["origin"],
                kind=row["kind"],
                title=row["title"],
                summary=row["summary"],
                root_cause=row["root_cause"],
                proposal_json=json.loads(row["proposal_json"] or "{}"),
                risk_level=row["risk_level"],
                status=row["status"],
                version=row["version"],
                stage_started_at=row["stage_started_at"],
                stage_deadline=row["stage_deadline"],
                attempt=row["attempt"],
                last_error_json=json.loads(row["last_error_json"] or "{}"),
                ranking_score=row["ranking_score"],
                sandbox_json=json.loads(row["sandbox_json"] or "{}"),
                votes_summary_json=json.loads(row["votes_summary_json"] or "{}"),
                rollback_point_id=row["rollback_point_id"],
                applied_task_id=row["applied_task_id"],
                applied_sha=row["applied_sha"],
                monitor_until=row["monitor_until"],
                memory_refs_json=json.loads(row["memory_refs_json"] or "[]"),
                decided_by=row["decided_by"],
                created_by=row["created_by"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                applied_at=row["applied_at"],
                resolved_at=row["resolved_at"],
            )

        return self._execute_with_retry(_get)

    def list_improvements(
        self,
        status: str | None = None,
        origin: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Improvement]:
        """List improvements with optional filters."""

        def _list(conn: sqlite3.Connection) -> list[Improvement]:
            query = "SELECT * FROM improvements WHERE 1=1"
            params: list[Any] = []
            if status is not None:
                query += " AND status = ?"
                params.append(status)
            if origin is not None:
                query += " AND origin = ?"
                params.append(origin)
            if kind is not None:
                query += " AND kind = ?"
                params.append(kind)
            query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(query, params).fetchall()
            improvements = []
            for row in rows:
                improvements.append(
                    Improvement(
                        id=row["id"],
                        origin=row["origin"],
                        kind=row["kind"],
                        title=row["title"],
                        summary=row["summary"],
                        root_cause=row["root_cause"],
                        proposal_json=json.loads(row["proposal_json"] or "{}"),
                        risk_level=row["risk_level"],
                        status=row["status"],
                        version=row["version"],
                        stage_started_at=row["stage_started_at"],
                        stage_deadline=row["stage_deadline"],
                        attempt=row["attempt"],
                        last_error_json=json.loads(row["last_error_json"] or "{}"),
                        ranking_score=row["ranking_score"],
                        sandbox_json=json.loads(row["sandbox_json"] or "{}"),
                        votes_summary_json=json.loads(row["votes_summary_json"] or "{}"),
                        rollback_point_id=row["rollback_point_id"],
                        applied_task_id=row["applied_task_id"],
                        applied_sha=row["applied_sha"],
                        monitor_until=row["monitor_until"],
                        memory_refs_json=json.loads(row["memory_refs_json"] or "[]"),
                        decided_by=row["decided_by"],
                        created_by=row["created_by"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                        applied_at=row["applied_at"],
                        resolved_at=row["resolved_at"],
                    )
                )
            return improvements

        return self._execute_with_retry(_list)

    def transition_improvement(
        self,
        imp_id: str,
        expected_status: str,
        new_status: str,
        actor: str,
        detail_json: dict[str, Any] | None = None,
    ) -> None:
        """CAS transition + atomic log append (§5b.1)."""

        def _transition(conn: sqlite3.Connection) -> None:
            # Fetch current state
            row = conn.execute(
                "SELECT status, version FROM improvements WHERE id = ?", (imp_id,)
            ).fetchone()
            if not row:
                raise TransitionConflict(f"Improvement {imp_id} not found")
            if row["status"] != expected_status:
                raise TransitionConflict(f"Expected status {expected_status}, got {row['status']}")

            # CAS: UPDATE WHERE status=? AND version=?, bump version
            now = utc_now_iso()
            cursor = conn.execute(
                """
                UPDATE improvements
                SET status = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND status = ? AND version = ?
                """,
                (new_status, now, imp_id, expected_status, row["version"]),
            )
            if cursor.rowcount != 1:
                raise TransitionConflict(
                    f"CAS failed for {imp_id}: {expected_status} → {new_status}"
                )

            # Append immutable log row
            log_params = _LogRowParams(
                entity_type="improvement",
                entity_id=imp_id,
                from_status=expected_status,
                to_status=new_status,
                actor=actor,
                ts=now,
                detail_json=detail_json,
            )
            self._log_row_to_db(conn, log_params)

        self._execute_with_retry(_transition)

    def update_improvement_fields(
        self,
        imp_id: str,
        **fields: Any,
    ) -> None:
        """Partial update to non-status fields."""

        def _update(conn: sqlite3.Connection) -> None:
            # Build dynamic UPDATE query
            safe_fields = {
                k: v
                for k, v in fields.items()
                if k
                in (
                    "risk_level",
                    "ranking_score",
                    "sandbox_json",
                    "votes_summary_json",
                    "last_error_json",
                    "rollback_point_id",
                    "applied_task_id",
                    "applied_sha",
                    "monitor_until",
                    "memory_refs_json",
                    "decided_by",
                    "stage_started_at",
                    "stage_deadline",
                    "attempt",
                    "applied_at",
                    "resolved_at",
                )
            }
            if not safe_fields:
                return

            set_clauses = ", ".join(f"{k} = ?" for k in safe_fields.keys())
            values = list(safe_fields.values()) + [utc_now_iso(), imp_id]
            conn.execute(
                f"UPDATE improvements SET {set_clauses}, updated_at = ? WHERE id = ?",
                values,
            )

        self._execute_with_retry(_update)

    # --- Judge votes (append-only, idempotent)

    def insert_vote(
        self,
        improvement_id: str,
        panel_attempt_id: str,
        judge_agent: str,
        model_family: str,
        verdict: str,
        scores_json: dict[str, Any] | None = None,
        reasoning: str = "",
        conditions: str = "",
        model: str = "",
    ) -> str:
        """Insert vote — APPEND-ONLY idempotency (§5b.7, §6 m3).

        A retried judge (same improvement + panel attempt + family) is a no-op:
        the FIRST recorded vote stands. REPLACE is forbidden here — it would let a
        retry erase a prior vote's identity/content (codex #16).
        """

        def _insert(conn: sqlite3.Connection) -> str:
            existing = conn.execute(
                "SELECT id FROM improvement_votes WHERE improvement_id = ? AND panel_attempt_id = ? AND model_family = ?",
                (improvement_id, panel_attempt_id, model_family),
            ).fetchone()
            if existing:
                return existing["id"]
            vote_id = new_id("vot")
            now = utc_now_iso()
            conn.execute(
                """
                INSERT INTO improvement_votes
                  (id, improvement_id, panel_attempt_id, judge_agent, model_family, model,
                   verdict, scores_json, reasoning, conditions, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    vote_id,
                    improvement_id,
                    panel_attempt_id,
                    judge_agent,
                    model_family,
                    model,
                    verdict,
                    json.dumps(scores_json or {}),
                    reasoning,
                    conditions,
                    now,
                ),
            )
            return vote_id

        return self._execute_with_retry(_insert)

    def get_vote(self, vote_id: str) -> ImprovementVote | None:
        """Fetch vote by id."""

        def _get(conn: sqlite3.Connection) -> ImprovementVote | None:
            row = conn.execute(
                "SELECT * FROM improvement_votes WHERE id = ?", (vote_id,)
            ).fetchone()
            if not row:
                return None
            return ImprovementVote(
                id=row["id"],
                improvement_id=row["improvement_id"],
                panel_attempt_id=row["panel_attempt_id"],
                judge_agent=row["judge_agent"],
                model_family=row["model_family"],
                model=row["model"],
                verdict=row["verdict"],
                scores_json=json.loads(row["scores_json"] or "{}"),
                reasoning=row["reasoning"],
                conditions=row["conditions"],
                created_at=row["created_at"],
            )

        return self._execute_with_retry(_get)

    def list_votes(
        self,
        improvement_id: str | None = None,
        panel_attempt_id: str | None = None,
        limit: int = 100,
    ) -> list[ImprovementVote]:
        """List votes with optional filters."""

        def _list(conn: sqlite3.Connection) -> list[ImprovementVote]:
            query = "SELECT * FROM improvement_votes WHERE 1=1"
            params: list[Any] = []
            if improvement_id is not None:
                query += " AND improvement_id = ?"
                params.append(improvement_id)
            if panel_attempt_id is not None:
                query += " AND panel_attempt_id = ?"
                params.append(panel_attempt_id)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            votes = []
            for row in rows:
                votes.append(
                    ImprovementVote(
                        id=row["id"],
                        improvement_id=row["improvement_id"],
                        panel_attempt_id=row["panel_attempt_id"],
                        judge_agent=row["judge_agent"],
                        model_family=row["model_family"],
                        model=row["model"],
                        verdict=row["verdict"],
                        scores_json=json.loads(row["scores_json"] or "{}"),
                        reasoning=row["reasoning"],
                        conditions=row["conditions"],
                        created_at=row["created_at"],
                    )
                )
            return votes

        return self._execute_with_retry(_list)

    # --- Audit lifecycle (append-only)

    def create_audit(
        self,
        kind: str,
        window_start: str,
        window_end: str,
    ) -> str:
        """Create audit in queued status."""

        def _create(conn: sqlite3.Connection) -> str:
            audit_id = new_id("aud")
            now = utc_now_iso()
            conn.execute(
                """
                INSERT INTO reliability_audits
                  (id, kind, status, window_start, window_end, started_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (audit_id, kind, AuditStatus.QUEUED.value, window_start, window_end, now),
            )
            return audit_id

        return self._execute_with_retry(_create)

    def get_audit(self, audit_id: str) -> ReliabilityAudit | None:
        """Fetch audit by id."""

        def _get(conn: sqlite3.Connection) -> ReliabilityAudit | None:
            row = conn.execute(
                "SELECT * FROM reliability_audits WHERE id = ?", (audit_id,)
            ).fetchone()
            if not row:
                return None
            return ReliabilityAudit(
                id=row["id"],
                kind=row["kind"],
                status=row["status"],
                window_start=row["window_start"],
                window_end=row["window_end"],
                stats_json=json.loads(row["stats_json"] or "{}"),
                findings=row["findings"],
                report_note_path=row["report_note_path"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
            )

        return self._execute_with_retry(_get)

    def list_audits(
        self,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReliabilityAudit]:
        """List audits with optional filters."""

        def _list(conn: sqlite3.Connection) -> list[ReliabilityAudit]:
            query = "SELECT * FROM reliability_audits WHERE 1=1"
            params: list[Any] = []
            if kind is not None:
                query += " AND kind = ?"
                params.append(kind)
            if status is not None:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(query, params).fetchall()
            audits = []
            for row in rows:
                audits.append(
                    ReliabilityAudit(
                        id=row["id"],
                        kind=row["kind"],
                        status=row["status"],
                        window_start=row["window_start"],
                        window_end=row["window_end"],
                        stats_json=json.loads(row["stats_json"] or "{}"),
                        findings=row["findings"],
                        report_note_path=row["report_note_path"],
                        started_at=row["started_at"],
                        finished_at=row["finished_at"],
                    )
                )
            return audits

        return self._execute_with_retry(_list)

    def start_audit(self, audit_id: str) -> None:
        """Transition audit from queued → running."""

        def _start(conn: sqlite3.Connection) -> None:
            utc_now_iso()
            conn.execute(
                """
                UPDATE reliability_audits
                SET status = ? WHERE id = ? AND status = ?
                """,
                (AuditStatus.RUNNING.value, audit_id, AuditStatus.QUEUED.value),
            )

        self._execute_with_retry(_start)

    def complete_audit(
        self,
        audit_id: str,
        stats_json: dict[str, Any] | None = None,
        findings: int = 0,
        report_note_path: str | None = None,
    ) -> None:
        """Transition audit from running → completed."""

        def _complete(conn: sqlite3.Connection) -> None:
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE reliability_audits
                SET status = ?, stats_json = ?, findings = ?, report_note_path = ?, finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    AuditStatus.COMPLETED.value,
                    json.dumps(stats_json or {}),
                    findings,
                    report_note_path,
                    now,
                    audit_id,
                    AuditStatus.RUNNING.value,
                ),
            )

        self._execute_with_retry(_complete)

    def fail_audit(self, audit_id: str, error: str = "") -> None:
        """Transition audit from running → failed."""

        def _fail(conn: sqlite3.Connection) -> None:
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE reliability_audits
                SET status = ?, stats_json = ?, finished_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    AuditStatus.FAILED.value,
                    json.dumps({"error": error}),
                    now,
                    audit_id,
                    AuditStatus.RUNNING.value,
                ),
            )

        self._execute_with_retry(_fail)

    # --- Lease (mutual exclusion for apply/rollback, §5b.3)

    def acquire_lease(
        self,
        key: str,
        owner: str,
        duration_seconds: int = 3600,
    ) -> str:
        """Acquire or renew lease; return fencing token."""
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")

        def _acquire(conn: sqlite3.Connection) -> str:
            token = uuid.uuid4().hex
            now_dt = datetime.now(UTC)
            now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            expires_at = (now_dt + timedelta(seconds=duration_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

            row = conn.execute(
                "SELECT value_json FROM reliability_state WHERE key = ?",
                (f"lease:{key}",),
            ).fetchone()

            if row:
                try:
                    previous = json.loads(row["value_json"])
                    if not isinstance(previous, dict):
                        raise ValueError("lease payload is not an object")
                    lease_exp = self._lease_expiry(previous["expires_at"])
                    previous_generation = int(previous.get("generation", 0))
                except (KeyError, TypeError, ValueError) as exc:
                    raise LeaseConflict(f"Lease {key} state is corrupt") from exc

                if previous.get("owner") != owner and lease_exp > now_dt:
                    raise LeaseConflict(f"Lease {key} held by {previous.get('owner')}")

                # A fresh token fences the same owner's prior invocation as well
                # as an expired prior owner.
                lease = {
                    "owner": owner,
                    "token": token,
                    "generation": previous_generation + 1,
                    "expires_at": expires_at,
                }
                conn.execute(
                    "UPDATE reliability_state SET value_json = ?, updated_at = ? WHERE key = ?",
                    (json.dumps(lease), now, f"lease:{key}"),
                )
            else:
                lease = {
                    "owner": owner,
                    "token": token,
                    "generation": 1,
                    "expires_at": expires_at,
                }
                conn.execute(
                    "INSERT INTO reliability_state (key, value_json, updated_at) VALUES (?, ?, ?)",
                    (f"lease:{key}", json.dumps(lease), now),
                )

            return token

        return self._execute_with_retry(_acquire)

    @staticmethod
    def _lease_expiry(value: Any) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("lease expiry must include a timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _validated_lease(
        row: sqlite3.Row | None,
        *,
        key: str,
        owner: str,
        token: str,
    ) -> dict[str, Any]:
        """Decode and validate a persisted lease without trusting corrupt state."""
        if not row:
            raise LeaseConflict(f"Lease {key} not found")
        try:
            lease = json.loads(row["value_json"])
            if not isinstance(lease, dict):
                raise ValueError("lease payload is not an object")
            expires_at = SqliteReliabilityStore._lease_expiry(lease["expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LeaseConflict(f"Lease {key} state is corrupt") from exc
        if lease.get("owner") != owner or lease.get("token") != token:
            raise LeaseConflict(f"Lease {key} token invalid")
        if expires_at <= datetime.now(UTC):
            raise LeaseConflict(f"Lease {key} expired")
        return lease

    def assert_lease(self, key: str, owner: str, token: str) -> None:
        """Fence a mutation by proving this exact token is still current."""

        def _assert(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT value_json FROM reliability_state WHERE key = ?",
                (f"lease:{key}",),
            ).fetchone()
            self._validated_lease(
                row,
                key=key,
                owner=owner,
                token=token,
            )

        self._execute_with_retry(_assert)

    def renew_lease(self, key: str, owner: str, token: str, duration_seconds: int = 3600) -> None:
        """Renew lease with valid token."""
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")

        def _renew(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT value_json FROM reliability_state WHERE key = ?",
                (f"lease:{key}",),
            ).fetchone()
            lease = self._validated_lease(
                row,
                key=key,
                owner=owner,
                token=token,
            )

            now = utc_now_iso()
            expires_at = (datetime.now(UTC) + timedelta(seconds=duration_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            lease["expires_at"] = expires_at
            conn.execute(
                "UPDATE reliability_state SET value_json = ?, updated_at = ? WHERE key = ?",
                (json.dumps(lease), now, f"lease:{key}"),
            )

        self._execute_with_retry(_renew)

    def release_lease(self, key: str, owner: str, token: str) -> None:
        """Release a held lease."""

        def _release(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT value_json FROM reliability_state WHERE key = ?",
                (f"lease:{key}",),
            ).fetchone()
            if row:
                try:
                    lease = json.loads(row["value_json"])
                except (TypeError, ValueError):
                    return
                if (
                    isinstance(lease, dict)
                    and lease.get("owner") == owner
                    and lease.get("token") == token
                ):
                    conn.execute(
                        "DELETE FROM reliability_state WHERE key = ?",
                        (f"lease:{key}",),
                    )

        self._execute_with_retry(_release)

    def reclaim_stale_lease(self, key: str, threshold_seconds: int = 600) -> bool:
        """Reclaim a lease expired for at least ``threshold_seconds``."""
        if threshold_seconds < 0:
            raise ValueError("threshold_seconds must be non-negative")

        def _reclaim(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT value_json FROM reliability_state WHERE key = ?",
                (f"lease:{key}",),
            ).fetchone()
            if not row:
                return False

            try:
                lease = json.loads(row["value_json"])
                if not isinstance(lease, dict):
                    raise ValueError("lease payload is not an object")
                lease_exp = self._lease_expiry(lease["expires_at"])
            except (KeyError, TypeError, ValueError) as exc:
                raise LeaseConflict(f"Lease {key} state is corrupt") from exc

            expired_for = (datetime.now(UTC) - lease_exp).total_seconds()
            if expired_for < threshold_seconds:
                return False

            conn.execute(
                "DELETE FROM reliability_state WHERE key = ?",
                (f"lease:{key}",),
            )
            return True

        return self._execute_with_retry(_reclaim)

    # --- Watch cursor (§5b.5)

    def get_watch_state(self) -> dict[str, Any]:
        """Return durable watcher state without inventing a heartbeat.

        ``updated_at`` is the liveness heartbeat; ``cursor`` is the scan
        high-water mark and can intentionally remain old after a failed scan.
        """

        def _get(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute(
                "SELECT value_json, updated_at FROM reliability_state WHERE key = ?",
                ("watch_cursor",),
            ).fetchone()
            if not row:
                return {
                    "state": "never_run",
                    "cursor_at": None,
                    "heartbeat_at": None,
                    "error": None,
                }
            heartbeat_at = str(row["updated_at"])
            try:
                payload = json.loads(row["value_json"])
                if not isinstance(payload, dict):
                    raise ValueError("watch payload is not an object")
                cursor_at = payload.get("cursor")
                if not isinstance(cursor_at, str) or not cursor_at:
                    raise ValueError("watch cursor is missing")
            except (TypeError, ValueError):
                return {
                    "state": "corrupt",
                    "cursor_at": None,
                    "heartbeat_at": heartbeat_at,
                    "error": "corrupt_watch_state",
                }
            return {
                "state": "recorded",
                "cursor_at": cursor_at,
                "heartbeat_at": heartbeat_at,
                "error": None,
            }

        return self._execute_with_retry(_get)

    def get_watch_cursor(self) -> str | None:
        """Fetch the watch high-water mark, or ``None`` when never run/corrupt."""
        cursor = self.get_watch_state().get("cursor_at")
        return str(cursor) if cursor is not None else None

    def advance_watch_cursor(self, new_cursor: str) -> None:
        """Advance watch cursor (call after all events in window durably written)."""

        def _advance(conn: sqlite3.Connection) -> None:
            now = utc_now_iso()
            row = conn.execute(
                "SELECT value_json FROM reliability_state WHERE key = ?",
                ("watch_cursor",),
            ).fetchone()
            first_seen = now
            if row:
                try:
                    previous = json.loads(row["value_json"])
                    if isinstance(previous, dict) and previous.get("first_seen"):
                        first_seen = str(previous["first_seen"])
                except (TypeError, ValueError):
                    pass
            cursor_val = {"cursor": new_cursor, "first_seen": first_seen}
            conn.execute(
                """
                INSERT INTO reliability_state (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = ?, updated_at = ?
                """,
                ("watch_cursor", json.dumps(cursor_val), now, json.dumps(cursor_val), now),
            )

        self._execute_with_retry(_advance)

    # --- Scorecards (upsert)

    def upsert_scorecard(
        self,
        subject_type: str,
        subject_id: str,
        window: str,
        period_start: str,
        metrics_json: dict[str, Any] | None = None,
    ) -> str:
        """Upsert scorecard; return id."""

        def _upsert(conn: sqlite3.Connection) -> str:
            scorecard_id = new_id("sc")
            now = utc_now_iso()
            conn.execute(
                """
                INSERT INTO scorecards
                  (id, subject_type, subject_id, window, period_start, metrics_json, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject_type, subject_id, window, period_start)
                DO UPDATE SET metrics_json = ?, computed_at = ?
                """,
                (
                    scorecard_id,
                    subject_type,
                    subject_id,
                    window,
                    period_start,
                    json.dumps(metrics_json or {}),
                    now,
                    json.dumps(metrics_json or {}),
                    now,
                ),
            )
            return scorecard_id

        return self._execute_with_retry(_upsert)

    def get_scorecard(
        self,
        subject_type: str,
        subject_id: str,
        window: str,
        period_start: str,
    ) -> Scorecard | None:
        """Fetch scorecard by composite key."""

        def _get(conn: sqlite3.Connection) -> Scorecard | None:
            row = conn.execute(
                """
                SELECT * FROM scorecards
                WHERE subject_type = ? AND subject_id = ? AND window = ? AND period_start = ?
                """,
                (subject_type, subject_id, window, period_start),
            ).fetchone()
            if not row:
                return None
            return Scorecard(
                id=row["id"],
                subject_type=row["subject_type"],
                subject_id=row["subject_id"],
                window=row["window"],
                period_start=row["period_start"],
                metrics_json=json.loads(row["metrics_json"] or "{}"),
                computed_at=row["computed_at"],
            )

        return self._execute_with_retry(_get)

    def list_scorecards(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        window: str | None = None,
        limit: int = 100,
    ) -> list[Scorecard]:
        """List scorecards with optional filters."""

        def _list(conn: sqlite3.Connection) -> list[Scorecard]:
            query = "SELECT * FROM scorecards WHERE 1=1"
            params: list[Any] = []
            if subject_type is not None:
                query += " AND subject_type = ?"
                params.append(subject_type)
            if subject_id is not None:
                query += " AND subject_id = ?"
                params.append(subject_id)
            if window is not None:
                query += " AND window = ?"
                params.append(window)
            query += " ORDER BY computed_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            scorecards = []
            for row in rows:
                scorecards.append(
                    Scorecard(
                        id=row["id"],
                        subject_type=row["subject_type"],
                        subject_id=row["subject_id"],
                        window=row["window"],
                        period_start=row["period_start"],
                        metrics_json=json.loads(row["metrics_json"] or "{}"),
                        computed_at=row["computed_at"],
                    )
                )
            return scorecards

        return self._execute_with_retry(_list)

    # --- Organization

    def create_org_unit(
        self,
        name: str,
        kind: str,
        parent_id: str | None = None,
        charter: str = "",
    ) -> str:
        """Create organization unit."""

        def _create(conn: sqlite3.Connection) -> str:
            unit_id = new_id("org")
            now = utc_now_iso()
            conn.execute(
                """
                INSERT INTO org_units (id, name, kind, parent_id, charter, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (unit_id, name, kind, parent_id, charter, now, now),
            )
            return unit_id

        return self._execute_with_retry(_create)

    def get_org_unit(self, unit_id: str) -> OrgUnit | None:
        """Fetch org_unit by id."""

        def _get(conn: sqlite3.Connection) -> OrgUnit | None:
            row = conn.execute("SELECT * FROM org_units WHERE id = ?", (unit_id,)).fetchone()
            if not row:
                return None
            return OrgUnit(
                id=row["id"],
                name=row["name"],
                kind=row["kind"],
                parent_id=row["parent_id"],
                charter=row["charter"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

        return self._execute_with_retry(_get)

    def list_org_units(
        self,
        kind: str | None = None,
        parent_id: str | None = None,
        status: str = "active",
    ) -> list[OrgUnit]:
        """List org_units with optional filters."""

        def _list(conn: sqlite3.Connection) -> list[OrgUnit]:
            query = "SELECT * FROM org_units WHERE status = ?"
            params: list[Any] = [status]
            if kind is not None:
                query += " AND kind = ?"
                params.append(kind)
            if parent_id is not None:
                query += " AND parent_id = ?"
                params.append(parent_id)

            rows = conn.execute(query, params).fetchall()
            units = []
            for row in rows:
                units.append(
                    OrgUnit(
                        id=row["id"],
                        name=row["name"],
                        kind=row["kind"],
                        parent_id=row["parent_id"],
                        charter=row["charter"],
                        status=row["status"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                )
            return units

        return self._execute_with_retry(_list)

    def create_agent(
        self,
        name: str,
        org_unit_id: str | None = None,
        org_role: str = "specialist",
        title: str = "",
        charter: str = "",
        model: str | None = None,
        harness: str | None = None,
        schedule_json: dict[str, Any] | None = None,
        vault_note_path: str | None = None,
    ) -> str:
        """Create agent with org columns."""

        def _create(conn: sqlite3.Connection) -> str:
            agent_id = new_id("agt")
            now = utc_now_iso()
            conn.execute(
                """
                INSERT INTO agents
                  (id, name, model, org_unit_id, org_role, title, charter, harness,
                   schedule_json, vault_note_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    name,
                    model,
                    org_unit_id,
                    org_role,
                    title,
                    charter,
                    harness,
                    json.dumps(schedule_json or {}),
                    vault_note_path,
                    now,
                    now,
                ),
            )
            return agent_id

        return self._execute_with_retry(_create)

    def get_agent(self, agent_id: str) -> Agent | None:
        """Fetch agent by id."""

        def _get(conn: sqlite3.Connection) -> Agent | None:
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if not row:
                return None
            return Agent(
                id=row["id"],
                name=row["name"],
                model=row["model"],
                org_unit_id=row["org_unit_id"],
                org_role=row["org_role"],
                title=row["title"],
                charter=row["charter"],
                schedule_json=json.loads(row["schedule_json"] or "{}"),
                harness=row["harness"],
                enabled=row["enabled"],
                vault_note_path=row["vault_note_path"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

        return self._execute_with_retry(_get)

    def list_agents(
        self,
        org_unit_id: str | None = None,
        org_role: str | None = None,
        enabled: int | None = None,
    ) -> list[Agent]:
        """List agents with optional filters."""

        def _list(conn: sqlite3.Connection) -> list[Agent]:
            query = "SELECT * FROM agents WHERE 1=1"
            params: list[Any] = []
            if org_unit_id is not None:
                query += " AND org_unit_id = ?"
                params.append(org_unit_id)
            if org_role is not None:
                query += " AND org_role = ?"
                params.append(org_role)
            if enabled is not None:
                query += " AND enabled = ?"
                params.append(enabled)

            rows = conn.execute(query, params).fetchall()
            agents = []
            for row in rows:
                agents.append(
                    Agent(
                        id=row["id"],
                        name=row["name"],
                        model=row["model"],
                        org_unit_id=row["org_unit_id"],
                        org_role=row["org_role"],
                        title=row["title"],
                        charter=row["charter"],
                        schedule_json=json.loads(row["schedule_json"] or "{}"),
                        harness=row["harness"],
                        enabled=row["enabled"],
                        vault_note_path=row["vault_note_path"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                )
            return agents

        return self._execute_with_retry(_list)

    def update_agent(self, agent_id: str, **fields: Any) -> None:
        """Partial update to agent."""

        def _update(conn: sqlite3.Connection) -> None:
            safe_fields = {
                k: v
                for k, v in fields.items()
                if k
                in (
                    "name",
                    "model",
                    "org_unit_id",
                    "org_role",
                    "title",
                    "charter",
                    "harness",
                    "enabled",
                    "schedule_json",
                    "vault_note_path",
                )
            }
            if not safe_fields:
                return

            set_clauses = ", ".join(f"{k} = ?" for k in safe_fields.keys())
            values = list(safe_fields.values()) + [utc_now_iso(), agent_id]
            conn.execute(
                f"UPDATE agents SET {set_clauses}, updated_at = ? WHERE id = ?",
                values,
            )

        self._execute_with_retry(_update)

    # --- Agent requests

    def create_agent_request(
        self,
        description: str,
        from_agent_id: str = "system",
        from_lane: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Create agent request (pending status).

        Args:
            description: Agent request description.
            from_agent_id: Authenticated/emitting principal in canonical form.
                Defaults to "system" for internal events without auth.
                Must pass validation: lane:*, loop:*, job:*, human:*, or system.
                Non-canonical spellings like agent:* are rejected.
            from_lane: Optional lane context.
            session_id: Optional session context.
            run_id: Optional run context.

        Returns: agent_request id.
        Raises: ValueError if from_agent_id is not canonical.
        """
        # Validate canonical identity spelling
        _validate_from_agent_id(from_agent_id)

        # Use from_agent_id as display requested_by
        display_requested_by = _normalize_requested_by(from_agent_id)

        def _create(conn: sqlite3.Connection) -> str:
            req_id = new_id("areq")
            now = utc_now_iso()
            conn.execute(
                """
                INSERT INTO agent_requests
                  (id, description, requested_by, from_agent_id, from_lane, session_id, run_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (req_id, description, display_requested_by, from_agent_id, from_lane, session_id, run_id, now, now),
            )
            return req_id

        return self._execute_with_retry(_create)

    def get_agent_request(self, req_id: str) -> AgentRequest | None:
        """Fetch agent request by id."""

        def _get(conn: sqlite3.Connection) -> AgentRequest | None:
            row = conn.execute("SELECT * FROM agent_requests WHERE id = ?", (req_id,)).fetchone()
            if not row:
                return None
            return AgentRequest(
                id=row["id"],
                description=row["description"],
                requested_by=row["requested_by"],
                status=row["status"],
                design_json=json.loads(row["design_json"] or "{}"),
                improvement_id=row["improvement_id"],
                agent_id=row["agent_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                from_agent_id=_row_value(row, "from_agent_id") or "system",
                from_lane=_row_value(row, "from_lane"),
                session_id=_row_value(row, "session_id"),
                run_id=_row_value(row, "run_id"),
            )

        return self._execute_with_retry(_get)

    def list_agent_requests(
        self,
        status: str | None = None,
        limit: int = 100,
    ) -> list[AgentRequest]:
        """List agent requests with optional filter."""

        def _list(conn: sqlite3.Connection) -> list[AgentRequest]:
            query = "SELECT * FROM agent_requests WHERE 1=1"
            params: list[Any] = []
            if status is not None:
                query += " AND status = ?"
                params.append(status)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            requests = []
            for row in rows:
                requests.append(
                    AgentRequest(
                        id=row["id"],
                        description=row["description"],
                        requested_by=row["requested_by"],
                        status=row["status"],
                        design_json=json.loads(row["design_json"] or "{}"),
                        improvement_id=row["improvement_id"],
                        agent_id=row["agent_id"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                        from_agent_id=_row_value(row, "from_agent_id") or "system",
                        from_lane=_row_value(row, "from_lane"),
                        session_id=_row_value(row, "session_id"),
                        run_id=_row_value(row, "run_id"),
                    )
                )
            return requests

        return self._execute_with_retry(_list)

    def update_agent_request_status(
        self,
        req_id: str,
        status: str,
        design_json: dict[str, Any] | None = None,
        improvement_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Update agent request status and related fields."""

        def _update(conn: sqlite3.Connection) -> None:
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE agent_requests
                SET status = ?, design_json = ?, improvement_id = ?, agent_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(design_json or {}),
                    improvement_id,
                    agent_id,
                    now,
                    req_id,
                ),
            )

        self._execute_with_retry(_update)

    # --- Autonomy settings (read-only from store)

    def get_autonomy_setting(self, scope_type: str, scope_id: str = "") -> AutonomySetting | None:
        """Fetch autonomy setting by scope."""

        def _get(conn: sqlite3.Connection) -> AutonomySetting | None:
            row = conn.execute(
                """
                SELECT * FROM autonomy_settings
                WHERE scope_type = ? AND scope_id = ?
                """,
                (scope_type, scope_id),
            ).fetchone()
            if not row:
                return None
            return AutonomySetting(
                id=row["id"],
                scope_type=row["scope_type"],
                scope_id=row["scope_id"],
                mode=row["mode"],
                max_auto_risk=row["max_auto_risk"],
                updated_by=row["updated_by"],
                updated_at=row["updated_at"],
            )

        return self._execute_with_retry(_get)

    def list_autonomy_settings(self) -> list[AutonomySetting]:
        """List all autonomy settings."""

        def _list(conn: sqlite3.Connection) -> list[AutonomySetting]:
            rows = conn.execute("SELECT * FROM autonomy_settings").fetchall()
            settings = []
            for row in rows:
                settings.append(
                    AutonomySetting(
                        id=row["id"],
                        scope_type=row["scope_type"],
                        scope_id=row["scope_id"],
                        mode=row["mode"],
                        max_auto_risk=row["max_auto_risk"],
                        updated_by=row["updated_by"],
                        updated_at=row["updated_at"],
                    )
                )
            return settings

        return self._execute_with_retry(_list)

    def upsert_autonomy_setting(
        self,
        scope_type: str,
        scope_id: str,
        mode: str,
        max_auto_risk: int,
        updated_by: str,
    ) -> AutonomySetting:
        """Write an autonomy scope row — human-token API surface ONLY (§6 M3).

        The pipeline never calls this; it is invoked exclusively by the
        X-Autonomy-Token-guarded PUT /api/autonomy route. Every write appends a
        Tier-P `reliability_log` row so mode changes are tamper-evident.
        """
        if mode not in {"approve", "auto"}:
            raise ValueError(f"invalid mode: {mode}")
        if max_auto_risk not in {0, 1, 2}:
            raise ValueError(f"invalid max_auto_risk: {max_auto_risk}")
        if scope_type not in {"global", "department", "agent", "kind"}:
            raise ValueError(f"invalid scope_type: {scope_type}")

        def _upsert(conn: sqlite3.Connection) -> AutonomySetting:
            now = utc_now_iso()
            existing = conn.execute(
                "SELECT id, mode, max_auto_risk FROM autonomy_settings WHERE scope_type = ? AND scope_id = ?",
                (scope_type, scope_id),
            ).fetchone()
            if existing:
                setting_id = existing["id"]
                from_status = f"{existing['mode']}/{existing['max_auto_risk']}"
                conn.execute(
                    "UPDATE autonomy_settings SET mode = ?, max_auto_risk = ?, updated_by = ?, updated_at = ?"
                    " WHERE id = ?",
                    (mode, max_auto_risk, updated_by, now, setting_id),
                )
            else:
                setting_id = new_id("aut")
                from_status = None
                conn.execute(
                    "INSERT INTO autonomy_settings (id, scope_type, scope_id, mode, max_auto_risk, updated_by, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (setting_id, scope_type, scope_id, mode, max_auto_risk, updated_by, now),
                )
            self._log_row_to_db(
                conn,
                _LogRowParams(
                    entity_type="autonomy",
                    entity_id=f"{scope_type}:{scope_id}",
                    from_status=from_status,
                    to_status=f"{mode}/{max_auto_risk}",
                    actor=updated_by,
                    detail_json={"setting_id": setting_id},
                    ts=now,
                ),
            )
            return AutonomySetting(
                id=setting_id,
                scope_type=scope_type,
                scope_id=scope_id,
                mode=mode,
                max_auto_risk=max_auto_risk,
                updated_by=updated_by,
                updated_at=now,
            )

        return self._execute_with_retry(_upsert)

    def verify_log_chain(self) -> dict[str, Any]:
        """Recompute the reliability_log hash chain end-to-end (§6 m3, AC-14).

        Read-only. Returns {"valid", "checked", "first_bad_id"}.
        """

        def _verify(conn: sqlite3.Connection) -> dict[str, Any]:
            rows = conn.execute("SELECT * FROM reliability_log ORDER BY id ASC").fetchall()
            prev = _GENESIS_HASH
            for index, row in enumerate(rows):
                row_dict = {
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "from_status": row["from_status"],
                    "to_status": row["to_status"],
                    "actor": row["actor"],
                    "detail_json": json.loads(row["detail_json"] or "{}"),
                    "ts": row["ts"],
                }
                expected = _hash_log_row(prev, row_dict)
                if row["prev_hash"] != prev or row["hash"] != expected:
                    return {"valid": False, "checked": index + 1, "first_bad_id": row["id"]}
                prev = row["hash"]
            return {"valid": True, "checked": len(rows), "first_bad_id": None}

        return self._execute_with_retry(_verify)

    def resolve_autonomy(
        self, agent_id: str | None = None, kind: str | None = None
    ) -> AutonomySetting:
        """Resolve effective autonomy (most-specific scope wins).

        Priority: agent > department (via agent's org_unit) > kind > global.
        Falls back to global default (approve/0).
        """

        def _resolve(conn: sqlite3.Connection) -> AutonomySetting:
            # Try agent-specific
            if agent_id:
                row = conn.execute(
                    "SELECT * FROM autonomy_settings WHERE scope_type = ? AND scope_id = ?",
                    ("agent", agent_id),
                ).fetchone()
                if row:
                    return AutonomySetting(
                        id=row["id"],
                        scope_type=row["scope_type"],
                        scope_id=row["scope_id"],
                        mode=row["mode"],
                        max_auto_risk=row["max_auto_risk"],
                        updated_by=row["updated_by"],
                        updated_at=row["updated_at"],
                    )

                # Try agent's department
                agent_row = conn.execute(
                    "SELECT org_unit_id FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
                if agent_row and agent_row["org_unit_id"]:
                    dept_row = conn.execute(
                        "SELECT * FROM autonomy_settings WHERE scope_type = ? AND scope_id = ?",
                        ("department", agent_row["org_unit_id"]),
                    ).fetchone()
                    if dept_row:
                        return AutonomySetting(
                            id=dept_row["id"],
                            scope_type=dept_row["scope_type"],
                            scope_id=dept_row["scope_id"],
                            mode=dept_row["mode"],
                            max_auto_risk=dept_row["max_auto_risk"],
                            updated_by=dept_row["updated_by"],
                            updated_at=dept_row["updated_at"],
                        )

            # Try kind-specific
            if kind:
                row = conn.execute(
                    "SELECT * FROM autonomy_settings WHERE scope_type = ? AND scope_id = ?",
                    ("kind", kind),
                ).fetchone()
                if row:
                    return AutonomySetting(
                        id=row["id"],
                        scope_type=row["scope_type"],
                        scope_id=row["scope_id"],
                        mode=row["mode"],
                        max_auto_risk=row["max_auto_risk"],
                        updated_by=row["updated_by"],
                        updated_at=row["updated_at"],
                    )

            # Global fallback
            row = conn.execute(
                "SELECT * FROM autonomy_settings WHERE scope_type = ? AND scope_id = ?",
                ("global", ""),
            ).fetchone()
            if row:
                return AutonomySetting(
                    id=row["id"],
                    scope_type=row["scope_type"],
                    scope_id=row["scope_id"],
                    mode=row["mode"],
                    max_auto_risk=row["max_auto_risk"],
                    updated_by=row["updated_by"],
                    updated_at=row["updated_at"],
                )

            # Fallback to default
            return AutonomySetting(
                id="aut_default",
                scope_type="global",
                scope_id="",
                mode=AutonomyMode.APPROVE.value,
                max_auto_risk=0,
                updated_by="system",
                updated_at=utc_now_iso(),
            )

        return self._execute_with_retry(_resolve)
