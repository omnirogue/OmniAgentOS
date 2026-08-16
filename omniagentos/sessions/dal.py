"""Thread-safe SQLite access for Session Bridge state."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable, Iterable
from enum import StrEnum
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any, cast

from omniagentos.contracts import ApprovalState, Events, new_id, utc_now_iso
from omniagentos.db.store import (
    _checked_fields,
    _connect,
    _insert_sql,
    _next_event_sequence,
    _row,
    _rows,
)

LOG = logging.getLogger(__name__)

_SESSION_COLUMNS = frozenset(
    {
        "id",
        "source",
        "project_dir",
        "provider",
        "session_ref",
        "state",
        "pid",
        "model",
        "title",
        "company_override",
        "agent_name",
        "agent_status",
        "agent_profile",
        "agent_session_id",
        "budget_usd_max",
        "cost_usd",
        "kill_requested",
        "last_activity_at",
        "created_at",
        "updated_at",
        "prompt",
        "error",
        "output_text",
        "idle_minutes",
        # WP2: the claude_accounts.id this session runs under (migration 045),
        # persisted by the supervisor at launch. Durable inflight attribution:
        # limit_state counts live sessions per account across every lane.
        "account_id",
        # P3: JSON array of the session's server-computed, validated granted write
        # roots (project root_dirs + allowed_dirs), frozen at spawn. NULL = no extra
        # scope beyond project_dir (pre-P3 behavior).
        "granted_roots",
    }
)
# Columns that exist on the sessions table but are NOT insertable by create_session:
# killed_by (13) is written only by the kill/terminalize paths, todos_json/files_json
# (034) only by the live-stream capture, and the usage telemetry (049) only by
# record_session_usage. Readers still need every one of them -- the API's
# _session_row pops todos_json/files_json to build the board's progress/stage/files,
# and killed_by drives the D7 operator-supersede classification -- so the READ set is
# _SESSION_COLUMNS plus these, never _SESSION_COLUMNS alone.
_SESSION_READ_ONLY_COLUMNS = frozenset(
    {
        "killed_by",
        "todos_json",
        "files_json",
        "effort",
        "input_tokens",
        "output_tokens",
        "wall_ms",
        "usage_source",
        # C4 / migration 118. An UPPER-BOUND estimate priced from the measured
        # tokens, kept strictly apart from `cost_usd` so NULL still means
        # "unpriced" and 0.0 still means "the provider said free". Written only
        # by record_session_usage; read by dollar-cap accrual.
        "cost_estimate_usd",
        "cost_estimate_source",
        # Attention routing (migration 136). Last-write-wins snapshot written
        # by POST /api/session-events/hook. NULL = no hook event recorded yet.
        "attention_state",
        "attention_reason",
        "attention_since",
    }
)
# The explicit projection every session list query uses instead of SELECT *. Derived
# from the two frozensets above so the read list and the insert allowlist can never
# drift apart, and sorted so the emitted SQL text is byte-stable across processes
# (frozenset iteration order is not) and hits SQLite's prepared-statement cache.
_SESSION_SELECT_COLUMNS = ", ".join(sorted(_SESSION_COLUMNS | _SESSION_READ_ONLY_COLUMNS))
# Session ids per batched spawn-error lookup. Well under SQLITE_MAX_VARIABLE_NUMBER
# (32766 since SQLite 3.32, 999 before it) so the batch stays one statement even for
# the limit=10_000 supervisor/reconciler callers.
_SPAWN_ERROR_BATCH = 500
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
        "session_id",
    }
)
_ACTIVITY_WRITE_INTERVAL_SECONDS = 5.0
_UNSET = object()


class SessionState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    RESUMING = "resuming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    KILLED = "killed"


SESSION_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.STARTING: frozenset(
        {
            SessionState.RUNNING,
            SessionState.AWAITING_APPROVAL,
            SessionState.COMPLETED,
            SessionState.FAILED,
            SessionState.CANCELLED,
            SessionState.KILLED,
        }
    ),
    SessionState.RUNNING: frozenset(
        {
            # An unambiguous dead-account 401 gets one fresh-account launch.
            SessionState.STARTING,
            SessionState.AWAITING_APPROVAL,
            SessionState.COMPLETED,
            SessionState.FAILED,
            SessionState.CANCELLED,
            SessionState.KILLED,
        }
    ),
    SessionState.AWAITING_APPROVAL: frozenset(
        {
            SessionState.RESUMING,
            SessionState.FAILED,
            SessionState.CANCELLED,
            SessionState.KILLED,
        }
    ),
    SessionState.RESUMING: frozenset(
        {
            SessionState.RUNNING,
            SessionState.AWAITING_APPROVAL,
            SessionState.COMPLETED,
            SessionState.FAILED,
            SessionState.CANCELLED,
            SessionState.KILLED,
        }
    ),
    SessionState.COMPLETED: frozenset(),
    SessionState.FAILED: frozenset(),
    SessionState.CANCELLED: frozenset(),
    SessionState.KILLED: frozenset(),
}
TERMINAL_SESSION_STATES = frozenset(
    {
        SessionState.COMPLETED,
        SessionState.FAILED,
        SessionState.CANCELLED,
        SessionState.KILLED,
    }
)


def encode_granted_roots(roots: Iterable[str] | None) -> str | None:
    """Serialize a session's granted write roots for the ``granted_roots`` column.

    Returns a JSON array string of the non-empty entries, or ``None`` when there is
    nothing to store (so the column stays NULL = "no extra scope beyond project_dir",
    the pre-P3 behavior). Order-preserving and deduplicated."""
    if not roots:
        return None
    seen: list[str] = []
    for raw in roots:
        value = str(raw).strip()
        if value and value not in seen:
            seen.append(value)
    return json.dumps(seen) if seen else None


def decode_granted_roots(session: Any) -> list[str]:
    """The session's persisted granted write roots (a list of realpaths), or [].

    The single reader for the ``granted_roots`` column, shared by the supervisor
    (to widen the OS sandbox) and the hook-eval route (to widen the classifier),
    so both derive the session's scope from the SAME frozen, server-computed value
    -- never from a hook payload. Fails closed to ``[]`` on any malformed value."""
    if not session:
        return []
    raw = session.get("granted_roots") if isinstance(session, dict) else None
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def can_transition_session(current: SessionState, target: SessionState) -> bool:
    return target in SESSION_TRANSITIONS[current]


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        dal = cast("SessionsDal", args[0])
        with dal._lock:
            return method(*args, **kwargs)

    return wrapped


class SessionsDal:
    """Auto-migrated, serialized sessions-table access.

    A path owns its connection, matching :class:`SqliteStore`. A supplied
    connection is useful for composition and tests and remains caller-owned.
    """

    def __init__(self, database: str | Path | sqlite3.Connection) -> None:
        self._lock = RLock()
        self._last_activity_write: dict[str, float] = {}
        # Migration 118 presence, resolved once per DAL (see
        # _has_cost_estimate_columns). None = not yet probed.
        self._cost_estimate_columns: bool | None = None
        if isinstance(database, sqlite3.Connection):
            self._connection = database
            self._connection.row_factory = sqlite3.Row
            self._owns_connection = False
            from omniagentos.db.migrate import migrate_connection

            migrate_connection(self._connection)
            self._ensure_orchestrator_marker()
        else:
            self._connection = _connect(str(database))
            self._owns_connection = True
            try:
                from omniagentos.db.migrate import migrate_connection

                migrate_connection(self._connection)
                self._ensure_orchestrator_marker()
            except BaseException:
                self._connection.close()
                raise

    def _ensure_orchestrator_marker(self) -> None:
        """Idempotently ensure the orchestrator-ownership marker table exists.

        Created here (CREATE TABLE IF NOT EXISTS) rather than as a numbered
        migration ON PURPOSE: it is purely additive, carries NO schema-version bump
        (the live DB stays at its current migration), and exists only to answer one
        question at the bridge hook — "did the Orchestrator spawn this session?" —
        so an orchestrator-owned session's approvals can auto-resolve (safe
        auto-approve / money+delete escalate) instead of parking for a human. A
        human-spawned bridge session is simply never marked, so it is unaffected.
        """
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS orchestrator_sessions ("
            "session_id TEXT PRIMARY KEY, run_id TEXT, created_at TEXT NOT NULL)"
        )
        self._connection.commit()

    @_serialized
    def close(self) -> None:
        if self._owns_connection:
            self._connection.close()
            self._owns_connection = False

    def _begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._connection.commit()

    def _rollback(self) -> None:
        self._connection.rollback()

    def _event(self, session_id: str, state: str, *, created: bool = False) -> None:
        # T-DESIGN-002 / AC-15: session LIFECYCLE transitions persist as Events.AUDIT
        # rows (NOT session.* types). Live progress is SSE-synthesized, never persisted,
        # so the audit log and SSE replay window are not flooded (DR-007).
        action = "session.created" if created else f"session.{state}"
        # W2.6: a session IS the execution unit for this lane (contracts.ExecutionRef:
        # "Lane stores ... sessions ... remain the only execution authorities"), so
        # execution_id = session_id. Caller always holds the writer's BEGIN IMMEDIATE
        # transaction here (create_session / update_session_state), so the sequence
        # read+insert below is atomic -- see _next_event_sequence.
        sequence = _next_event_sequence(self._connection, session_id)
        self._connection.execute(
            "INSERT INTO events "
            "(ts, type, actor, action, target_type, target_id, payload_json, trace_id, "
            "execution_id, sequence) "
            "VALUES (?, ?, 'session-bridge', ?, 'session', ?, ?, '', ?, ?)",
            (
                utc_now_iso(),
                Events.AUDIT,
                action,
                session_id,
                json.dumps({"state": state}, sort_keys=True, separators=(",", ":")),
                session_id,
                sequence,
            ),
        )

    def _attach_session_error(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        """Attach the latest durable spawn error without requiring a schema change."""
        if row is None:
            return None
        event = self._connection.execute(
            "SELECT payload_json FROM events "
            "WHERE target_type = 'session' AND target_id = ? "
            "AND action = 'session.spawn_failed' ORDER BY id DESC LIMIT 1",
            (str(row["id"]),),
        ).fetchone()
        if event is None:
            return row
        try:
            payload = json.loads(str(event["payload_json"] or "{}"))
        except json.JSONDecodeError:
            return row
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, str) and error:
            row["error"] = error
        return row

    def _latest_spawn_errors(self, session_ids: Iterable[str]) -> dict[str, str]:
        """Latest durable spawn error per session id, in ONE query per batch.

        The single-row :meth:`_attach_session_error` is correct but runs one events
        query PER row, so a 200-session list cost 201 round trips. The grouped
        ``MAX(id)`` subquery picks the newest ``session.spawn_failed`` event for every
        requested id at once and rides the same ``idx_events_target`` index
        (target_type, target_id, id DESC) the per-row query used. Decoding matches the
        single-row path exactly: a payload that will not parse, or whose ``error`` is
        missing/empty/not a string, contributes no entry -- the caller's row is then
        left untouched.
        """
        unique = [str(i) for i in dict.fromkeys(session_ids) if i]
        errors: dict[str, str] = {}
        for start in range(0, len(unique), _SPAWN_ERROR_BATCH):
            chunk = unique[start : start + _SPAWN_ERROR_BATCH]
            placeholders = ",".join("?" * len(chunk))
            rows = self._connection.execute(
                "SELECT target_id, payload_json FROM events WHERE id IN ("
                "SELECT MAX(id) FROM events "
                "WHERE target_type = 'session' AND action = 'session.spawn_failed' "
                f"AND target_id IN ({placeholders}) GROUP BY target_id)",
                chunk,
            ).fetchall()
            for raw in rows:
                try:
                    payload = json.loads(str(raw["payload_json"] or "{}"))
                except json.JSONDecodeError:
                    continue
                error = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(error, str) and error:
                    errors[str(raw["target_id"])] = error
        return errors

    def _attach_session_errors(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Batched :meth:`_attach_session_error` for list queries. Mutates in place."""
        if not rows:
            return rows
        errors = self._latest_spawn_errors(
            str(row["id"]) for row in rows if row.get("id") is not None
        )
        if not errors:
            return rows
        for row in rows:
            error = errors.get(str(row.get("id")))
            if error is not None:
                row["error"] = error
        return rows

    @_serialized
    def create_session(self, row: dict[str, Any]) -> None:
        values = dict(row)
        now = utc_now_iso()
        values.setdefault("provider", "claude")
        values.setdefault("state", SessionState.STARTING.value)
        # NULL seed (migration 088): a 0.0 here is a claim of "spent nothing".
        # Callers that still pass cost_usd=0.0 keep that value until a
        # tokens-only usage record clears it; the swarm budget ledger also
        # treats that usage marker as unknown for legacy rows.
        values.setdefault("cost_usd", None)
        values.setdefault("kill_requested", 0)
        values.setdefault("created_at", now)
        values.setdefault("updated_at", now)
        if not str(values.get("id", "")).startswith("ses_"):
            raise ValueError("session id must use the ses_ prefix")
        SessionState(str(values["state"]))
        checked = _checked_fields(values, _SESSION_COLUMNS)
        sql, parameters = _insert_sql("sessions", checked)
        self._begin()
        try:
            self._connection.execute(sql, parameters)
            self._event(str(values["id"]), str(values["state"]), created=True)
            self._commit()
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._attach_session_error(
            _row(
                self._connection.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
            )
        )

    @_serialized
    def get_claude_account(self, account_id: str) -> dict[str, Any] | None:
        """The claude_accounts row for ``account_id`` (config_dir resolution).

        Read-only lookup used by the live-transcript resolution
        (:mod:`omniagentos.sessions.transcript`): a claude-bridge session's
        CLI transcript lives under its account's ``config_dir``.
        """
        return _row(
            self._connection.execute(
                "SELECT * FROM claude_accounts WHERE id = ?", (account_id,)
            ).fetchone()
        )

    @_serialized
    def get_sessions_by_ids(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Batch-fetch sessions by id in ONE query, keyed by id.

        Board reconciliation links many sessions per poll; doing N ``get_session``
        round-trips (and, worse, opening a fresh migrating connection per board load)
        collapsed under concurrent-session write load. This is the single-query path."""
        unique = [i for i in dict.fromkeys(ids) if i]
        if not unique:
            return {}
        placeholders = ",".join("?" * len(unique))
        rows = self._connection.execute(
            f"SELECT * FROM sessions WHERE id IN ({placeholders})", unique
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for raw in rows:
            row = _row(raw)
            if row is not None and row.get("id") is not None:
                result[str(row["id"])] = row
        return result

    @_serialized
    def get_session_by_ref(self, ref: str) -> dict[str, Any] | None:
        """Resolve a session by canonical id OR provider session_ref (T-DESIGN-006)."""
        return self._attach_session_error(
            _row(
                self._connection.execute(
                    "SELECT * FROM sessions WHERE id = ? OR session_ref = ? "
                    "ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END LIMIT 1",
                    (ref, ref, ref),
                ).fetchone()
            )
        )

    @_serialized
    def mark_orchestrator_session(self, session_id: str, run_id: str | None = None) -> None:
        """Record that the Orchestrator (not a human) spawned ``session_id``.

        Idempotent (``INSERT OR IGNORE``): the marker is a durable, cross-process
        signal the bridge hook reads to auto-resolve this session's approvals.
        """
        self._begin()
        try:
            self._connection.execute(
                "INSERT OR IGNORE INTO orchestrator_sessions "
                "(session_id, run_id, created_at) VALUES (?, ?, ?)",
                (session_id, run_id, utc_now_iso()),
            )
            self._commit()
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def is_orchestrator_session(self, session_id: str) -> bool:
        """Return True iff ``session_id`` was spawned by the Orchestrator."""
        return (
            self._connection.execute(
                "SELECT 1 FROM orchestrator_sessions WHERE session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
            is not None
        )

    @_serialized
    def orchestrator_session_run(self, session_id: str) -> str | None:
        """The run id recorded as ``session_id``'s durable owner, if any.

        Reads ``orchestrator_sessions.run_id`` — the binding written at spawn
        time (provider_exec / supervisor / executor all mark with the run id
        that spawned the session). ``None`` means "no proven owner": either no
        row (never orchestrator-spawned, or spawned before marking existed) or
        a NULL ``run_id`` (marked without an owner). Destructive fan-outs such
        as the swarm cancel route treat ``None`` and a MISMATCH identically:
        refuse to signal, report, fail closed — ``swarm_attempts.session_id``
        has no FK, so a stale/corrupt/mis-bound attempt row must never be
        enough to kill a bystander session.
        """
        row = self._connection.execute(
            "SELECT run_id FROM orchestrator_sessions WHERE session_id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None or row["run_id"] is None:
            return None
        return str(row["run_id"])

    @_serialized
    def list_sessions(self, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        if state is None:
            # Named columns, not SELECT *, so the row shape is a declared contract
            # (see _SESSION_SELECT_COLUMNS). idx_sessions_created (migration 055)
            # serves the ORDER BY, so no temp b-tree sort of the whole table.
            rows = _rows(
                self._connection.execute(
                    f"SELECT {_SESSION_SELECT_COLUMNS} FROM sessions "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            )
            return self._attach_session_errors(rows)
        SessionState(state)
        rows = _rows(
            self._connection.execute(
                f"SELECT {_SESSION_SELECT_COLUMNS} FROM sessions WHERE state = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (state, limit),
            ).fetchall()
        )
        return self._attach_session_errors(rows)

    @_serialized
    def list_live_sessions(self, limit: int = 500) -> list[dict[str, Any]]:
        """Non-terminal sessions (any provider/source) for board projection.

        Used by external multi-provider discovery to ensure every active
        terminal has a Kanban card, including process-discovered externals.
        """
        if limit < 1:
            return []
        states = tuple(s.value for s in SessionState if s not in TERMINAL_SESSION_STATES)
        placeholders = ",".join("?" * len(states))
        rows = _rows(
            self._connection.execute(
                f"SELECT {_SESSION_SELECT_COLUMNS} FROM sessions "
                f"WHERE state IN ({placeholders}) "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (*states, limit),
            ).fetchall()
        )
        return self._attach_session_errors(rows)

    @_serialized
    def get_session_by_pid(self, pid: int) -> dict[str, Any] | None:
        """Most recent non-terminal session row for a live process id, if any."""
        if pid <= 0:
            return None
        states = tuple(s.value for s in SessionState if s not in TERMINAL_SESSION_STATES)
        placeholders = ",".join("?" * len(states))
        return self._attach_session_error(
            _row(
                self._connection.execute(
                    f"SELECT {_SESSION_SELECT_COLUMNS} FROM sessions "
                    f"WHERE pid = ? AND state IN ({placeholders}) "
                    "ORDER BY created_at DESC, id DESC LIMIT 1",
                    (pid, *states),
                ).fetchone()
            )
        )

    @_serialized
    def record_session_error(self, session_id: str, error: str) -> None:
        """Persist a spawn failure in the audit log for API/dashboard visibility."""
        self._begin()
        try:
            sequence = _next_event_sequence(self._connection, session_id)
            self._connection.execute(
                "INSERT INTO events "
                "(ts, type, actor, action, target_type, target_id, payload_json, trace_id, "
                "execution_id, sequence) "
                "VALUES (?, ?, 'session-bridge', 'session.spawn_failed', "
                "'session', ?, ?, '', ?, ?)",
                (
                    utc_now_iso(),
                    Events.AUDIT,
                    session_id,
                    json.dumps(
                        {"state": SessionState.FAILED.value, "error": error},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    session_id,
                    sequence,
                ),
            )
            self._commit()
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def set_session_error(self, session_id: str, error_text: str | None) -> bool:
        """Persist a terminal error so the board's ``work.error`` can show it, or None to clear."""
        return self._update_fields(session_id, {"error": error_text})

    @_serialized
    def set_session_output(self, session_id: str, output_text: str | None) -> bool:
        """Persist provider output for the shared executor terminal contract."""
        return self._update_fields(session_id, {"output_text": output_text})

    @_serialized
    def clear_session_error(self, session_id: str) -> bool:
        """Clear any persisted error (e.g., on successful completion)."""
        return self._update_fields(session_id, {"error": None})

    @_serialized
    def awaiting_since(self, session_id: str) -> str | None:
        """Timestamp of the LAST transition into ``awaiting_approval``, or None.

        Read from the lifecycle audit events (``_event``) rather than from
        ``sessions.updated_at`` because the row's stamp moves on any field write
        (cost, pid, error, account attribution) — the supervisor's max-park
        ceiling needs the moment the park STARTED, and a park clock that any
        unrelated write silently rewinds is exactly the hang this guards.
        Re-entering the state (resume → park again) legitimately restarts it.
        """
        row = self._connection.execute(
            "SELECT ts FROM events WHERE target_type = 'session' AND target_id = ? "
            "AND action = ? ORDER BY id DESC LIMIT 1",
            (session_id, f"session.{SessionState.AWAITING_APPROVAL.value}"),
        ).fetchone()
        if row is None:
            return None
        value = row["ts"]
        return str(value) if value else None

    @_serialized
    def update_session_state(
        self,
        session_id: str,
        target: str,
        expect: str | Iterable[str] | None = None,
    ) -> bool:
        wanted = SessionState(target)
        expected = {expect} if isinstance(expect, str) else set(expect or ())
        if expected:
            for state in expected:
                SessionState(state)
        self._begin()
        try:
            row = self._connection.execute(
                "SELECT state FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                self._commit()
                return False
            current = SessionState(str(row["state"]))
            if (expected and current.value not in expected) or not can_transition_session(
                current, wanted
            ):
                self._commit()
                return False
            now = utc_now_iso()
            cursor = self._connection.execute(
                "UPDATE sessions SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
                (wanted.value, now, session_id, current.value),
            )
            if cursor.rowcount != 1:
                self._commit()
                return False
            if wanted in TERMINAL_SESSION_STATES:
                self._clear_attention_in_transaction(session_id)
            self._event(session_id, wanted.value)
            self._commit()
            return True
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def set_pid(self, session_id: str, pid: int | None) -> bool:
        return self._update_fields(session_id, {"pid": pid})

    @_serialized
    def set_session_ref(self, session_id: str, session_ref: str) -> bool:
        return self._update_fields(session_id, {"session_ref": session_ref})

    @_serialized
    def add_cost(self, session_id: str, cost_usd: float) -> bool:
        if cost_usd < 0:
            return False
        self._begin()
        try:
            # COALESCE: a NULL seed (migration 088 / tokens-only clear) must
            # not poison stream accumulation — NULL + x is NULL in SQL.
            cursor = self._connection.execute(
                "UPDATE sessions SET cost_usd = COALESCE(cost_usd, 0) + ?, "
                "updated_at = ? WHERE id = ?",
                (cost_usd, utc_now_iso(), session_id),
            )
            self._commit()
            return cursor.rowcount > 0
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def set_cost_usd(self, session_id: str, cost_usd: float) -> bool:
        """Replace cost_usd (not accumulate) to avoid double-counting on resume."""
        if cost_usd < 0:
            return False
        return self._update_fields(session_id, {"cost_usd": cost_usd})

    @_serialized
    def set_session_todos(self, session_id: str, todos_json: str) -> bool:
        """Store the session's latest TodoWrite checklist (a JSON array string).

        Captured from the live stream by the supervisor so the board can render a
        real progress bar. Last-write-wins: each TodoWrite call replaces the prior
        snapshot, which is exactly the checklist's current state."""
        return self._update_fields(session_id, {"todos_json": todos_json})

    @_serialized
    def set_session_files(self, session_id: str, files_json: str) -> bool:
        """Store the set of files the session has written/edited (a JSON array
        string), so the task detail can show which files are involved."""
        return self._update_fields(session_id, {"files_json": files_json})

    @_serialized
    def set_session_attention(
        self,
        session_id: str,
        *,
        attention_state: str | None,
        attention_reason: str | None,
        attention_since: str | None,
    ) -> bool:
        """Last-write-wins attention snapshot (migration 136).

        Touches ``updated_at`` so the eventbus tailer synthesizes
        ``session.updated`` (SSE-only; never persisted as an event type).
        Does not touch ``last_activity_at`` — the idle reaper's primary
        activity mark (supervisor._session_idle_measurement).
        """
        return self._update_fields(
            session_id,
            {
                "attention_state": attention_state,
                "attention_reason": attention_reason,
                "attention_since": attention_since,
            },
        )

    def _clear_attention_in_transaction(self, session_id: str) -> None:
        """NULL attention_* on a terminal transition (ghost needs_input)."""
        try:
            self._connection.execute(
                "UPDATE sessions SET attention_state = NULL, attention_reason = NULL, "
                "attention_since = NULL WHERE id = ?",
                (session_id,),
            )
        except sqlite3.OperationalError:
            # Pre-136 schema: columns do not exist yet.
            return

    @_serialized
    def set_idle_minutes(self, session_id: str, idle_minutes: float | None) -> bool:
        """Persist a per-session idle-timeout override (WP5b swarm spawns).

        The reaper honors ``sessions.idle_minutes`` over its global default;
        the swarm spawner writes the scheduler's tiered timeout here so the A2
        reaper enforces scheduler policy instead of racing it."""
        if idle_minutes is not None and idle_minutes <= 0:
            return False
        return self._update_fields(session_id, {"idle_minutes": idle_minutes})

    @_serialized
    def set_account_id(self, session_id: str, account_id: str | None) -> bool:
        """Persist which account this session runs under (WP2 durable inflight).

        Written by the supervisor at launch (including account switches on the
        auth-retry relaunch path -- last write wins, matching the process's
        actual credential). ``limit_state`` counts live sessions per account
        through this column."""
        return self._update_fields(session_id, {"account_id": account_id})

    @_serialized
    def update_session_details(
        self,
        session_id: str,
        *,
        title: Any = _UNSET,
        company_override: Any = _UNSET,
    ) -> bool:
        """Apply only explicitly supplied operator-editable session fields."""
        fields: dict[str, Any] = {}
        if title is not _UNSET:
            fields["title"] = title
        if company_override is not _UNSET:
            fields["company_override"] = company_override
        return bool(fields) and self._update_fields(session_id, fields)

    @_serialized
    def set_agent_view(
        self,
        session_id: str,
        *,
        name: str | None,
        status: str | None,
        profile: str | None,
        session_id_value: str | None,
    ) -> bool:
        """Persist the current Claude Agent View enrollment for an external PID."""
        return self._update_fields(
            session_id,
            {
                "agent_name": name,
                "agent_status": status,
                "agent_profile": profile,
                "agent_session_id": session_id_value,
            },
        )

    @_serialized
    def touch_activity(self, session_id: str, iso: str) -> bool:
        now = time.monotonic()
        previous = self._last_activity_write.get(session_id)
        if previous is not None and now - previous < _ACTIVITY_WRITE_INTERVAL_SECONDS:
            return False
        changed = self._update_fields(session_id, {"last_activity_at": iso})
        if changed:
            self._last_activity_write[session_id] = now
        return changed

    @_serialized
    def request_kill(self, session_id: str, *, killed_by: str | None = None) -> bool:
        """Set kill_requested=1 only for non-terminal sessions.

        The non-terminal WHERE clause is the CAS guard the A2 reaper relies on: a
        session that reached a terminal state between the reaper's idle check and
        this call is left untouched and the method returns False. ``killed_by``
        optionally records the requester's identity (e.g. ``idle-reaper``) so the
        supervisor's kill path can attribute the terminal row; omitted, behavior is
        byte-identical to before (killed_by stays untouched).

        FIRST ATTRIBUTION WINS: when ``kill_requested`` is already 1 the existing
        ``killed_by`` is preserved — a later reaper request must not overwrite an
        operator's ``cancel_requested`` (D7 would then respawn an operator-cancelled
        task). The call remains a no-op-but-True for already-requested kills on
        non-terminal sessions.
        """
        self._begin()
        try:
            if killed_by is None:
                cursor = self._connection.execute(
                    "UPDATE sessions SET kill_requested = 1, updated_at = ? "
                    "WHERE id = ? AND state NOT IN ('completed', 'failed', 'cancelled', 'killed')",
                    (utc_now_iso(), session_id),
                )
            else:
                cursor = self._connection.execute(
                    "UPDATE sessions SET "
                    "killed_by = CASE WHEN kill_requested = 1 THEN killed_by ELSE ? END, "
                    "kill_requested = 1, updated_at = ? "
                    "WHERE id = ? AND state NOT IN ('completed', 'failed', 'cancelled', 'killed')",
                    (killed_by, utc_now_iso(), session_id),
                )
            self._commit()
            return cursor.rowcount > 0
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def request_cancel(self, session_id: str) -> bool:
        """Durably request process termination with a CANCELLED final state.

        The state intentionally remains non-terminal until the supervisor has
        stopped the process. Repeated requests are successful and harmless.
        """
        self._begin()
        try:
            row = self._connection.execute(
                "SELECT state, kill_requested, killed_by FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                self._commit()
                return False
            state = SessionState(str(row["state"]))
            if state == SessionState.CANCELLED:
                self._commit()
                return True
            if state in TERMINAL_SESSION_STATES:
                self._commit()
                return False
            if bool(row["kill_requested"]) and row["killed_by"] == "cancel_requested":
                self._commit()
                return True
            self._connection.execute(
                "UPDATE sessions SET kill_requested = 1, killed_by = 'cancel_requested', "
                "updated_at = ? WHERE id = ?",
                (utc_now_iso(), session_id),
            )
            self._commit()
            return True
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def enqueue_message(
        self, session_id: str, message: str, *, created_by: str = "operator"
    ) -> dict[str, str]:
        """Durably queue an operator instruction for the bridge's next turn."""
        message_id = new_id("smsg")
        queued_at = utc_now_iso()
        self._begin()
        try:
            self._connection.execute(
                "INSERT INTO session_messages "
                "(id, session_id, message, queued_at, applied_at, created_by) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                (message_id, session_id, message, queued_at, created_by),
            )
            # session.updated is SSE-synthesized from updated_at. Touching it
            # makes queued operator guidance visible immediately to listeners.
            self._connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (queued_at, session_id)
            )
            self._commit()
            return {"id": message_id, "queued_at": queued_at}
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def claim_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Return unapplied operator messages for a bridge turn to consume."""
        return _rows(
            self._connection.execute(
                "SELECT * FROM session_messages WHERE session_id = ? AND applied_at IS NULL "
                "ORDER BY queued_at ASC, id ASC",
                (session_id,),
            ).fetchall()
        )

    @_serialized
    def list_session_messages(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM session_messages WHERE session_id = ? "
                "ORDER BY queued_at DESC, id DESC LIMIT ?",
                (session_id, max(1, min(limit, 200))),
            ).fetchall()
        )

    @_serialized
    def mark_session_message_applied(self, message_id: str) -> bool:
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE session_messages SET applied_at = ? WHERE id = ? AND applied_at IS NULL",
                (utc_now_iso(), message_id),
            )
            self._commit()
            return cursor.rowcount == 1
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def claim_and_mark_session_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Atomically claim and mark unapplied operator messages as applied.

        SELECT + UPDATE in one BEGIN IMMEDIATE transaction ensures that
        concurrent claimers never double-deliver: only one caller wins the
        applied_at stamp for each message."""
        self._begin()
        try:
            # Fetch unapplied messages
            rows = self._connection.execute(
                "SELECT * FROM session_messages WHERE session_id = ? AND applied_at IS NULL "
                "ORDER BY queued_at ASC, id ASC",
                (session_id,),
            ).fetchall()
            messages = _rows(rows)

            if messages:
                # Mark them applied atomically
                now = utc_now_iso()
                message_ids = [msg["id"] for msg in messages]
                placeholders = ",".join("?" * len(message_ids))
                self._connection.execute(
                    f"UPDATE session_messages SET applied_at = ? "
                    f"WHERE id IN ({placeholders}) AND applied_at IS NULL",
                    [now] + message_ids,
                )

            self._commit()

            from omniagentos.sessions.steering_marker import (
                clear_steering_pending,
                mark_steering_pending,
            )

            clear_steering_pending(session_id)
            # An enqueue racing the clear has already committed its SQLite row
            # before it writes the marker. Re-checking durable state here ensures
            # the drain never erases that newer signal.
            still_pending = self._connection.execute(
                "SELECT 1 FROM session_messages "
                "WHERE session_id = ? AND applied_at IS NULL LIMIT 1",
                (session_id,),
            ).fetchone()
            if still_pending is not None:
                mark_steering_pending(session_id)
            return messages
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def clear_kill(self, session_id: str) -> bool:
        return self._update_fields(session_id, {"kill_requested": 0})

    def evict_activity(self, session_id: str) -> None:
        """Remove session from in-memory activity coalesce map on terminal."""
        self._last_activity_write.pop(session_id, None)

    def _update_fields(self, session_id: str, fields: dict[str, Any]) -> bool:
        assignments = ", ".join(f"{column} = ?" for column in fields)
        parameters = [*fields.values(), utc_now_iso(), session_id]
        self._begin()
        try:
            cursor = self._connection.execute(
                f"UPDATE sessions SET {assignments}, updated_at = ? WHERE id = ?", parameters
            )
            self._commit()
            return cursor.rowcount > 0
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def create_session_approval(
        self,
        session_id: str,
        action_hash: str,
        action_class: str,
        proposed_action: str,
        params_json: str,
        risk: str,
        evidence: str,
        expires_at: str | None,
    ) -> str:
        try:
            decoded: Any = json.loads(params_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("params_json must be valid JSON") from exc
        params = decoded if isinstance(decoded, dict) else {"value": decoded}
        params["action_hash"] = action_hash
        encoded = json.dumps(params, sort_keys=True, separators=(",", ":"))
        self._begin()
        try:
            session = self._connection.execute(
                "SELECT id FROM sessions WHERE id = ? OR session_ref = ? "
                "ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END LIMIT 1",
                (session_id, session_id, session_id),
            ).fetchone()
            if session is None:
                raise ValueError(f"unknown session {session_id!r}")
            canonical_session_id = str(session["id"])
            pending = self._connection.execute(
                "SELECT id, params_json FROM approvals "
                "WHERE session_id = ? AND state = 'pending' ORDER BY created_at DESC, id DESC",
                (canonical_session_id,),
            ).fetchall()
            for row in pending:
                try:
                    existing = json.loads(str(row["params_json"]))
                except json.JSONDecodeError:
                    continue
                if isinstance(existing, dict) and existing.get("action_hash") == action_hash:
                    self._commit()
                    return str(row["id"])
            approval_id = new_id("apr")
            values = {
                "id": approval_id,
                "run_id": None,
                "task_id": None,
                "step_seq": None,
                "action_class": action_class,
                "proposed_action": proposed_action,
                "params_json": encoded,
                "risk": risk,
                "evidence": evidence,
                "state": ApprovalState.PENDING.value,
                "decided_by": None,
                "decision_note": None,
                "decided_at": None,
                "expires_at": expires_at,
                "created_at": utc_now_iso(),
                "session_id": canonical_session_id,
            }
            checked = _checked_fields(values, _APPROVAL_COLUMNS)
            sql, parameters = _insert_sql("approvals", checked)
            self._connection.execute(sql, parameters)
            # T-DESIGN-003: emit approval.requested in the SAME transaction as the
            # single writer so the dashboard's existing approvals SSE surfaces the
            # session approval within 5s (AC-1). Only fires on a NEW approval (the
            # dedup path above returns early without re-emitting).
            # W2.6: scoped to the owning session's execution_id (not approval_id) so
            # a session's approval events interleave, gap-free, with its lifecycle
            # events in one execution timeline.
            sequence = _next_event_sequence(self._connection, canonical_session_id)
            self._connection.execute(
                "INSERT INTO events "
                "(ts, type, actor, action, target_type, target_id, payload_json, trace_id, "
                "execution_id, sequence) "
                "VALUES (?, ?, 'session-bridge', 'requested', 'approval', ?, ?, '', ?, ?)",
                (
                    utc_now_iso(),
                    Events.APPROVAL_REQUESTED,
                    approval_id,
                    json.dumps(
                        {
                            "approval_id": approval_id,
                            "session_id": canonical_session_id,
                            "action_class": action_class,
                            "state": ApprovalState.PENDING.value,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    canonical_session_id,
                    sequence,
                ),
            )
            self._commit()
            return approval_id
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def get_approval_by_id(self, approval_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        )

    @_serialized
    def consume_authorized_approval(
        self,
        *,
        session_id: str,
        action_hash: str,
        now_iso: str,
        gate: Callable[[dict[str, Any]], bool],
    ) -> str | None:
        """Atomically find and single-use-consume a one-shot authorization (SEC-006).

        Returns the consumed approval id iff THIS session has an APPROVED, unexpired,
        not-yet-consumed approval whose stored ``action_hash`` matches AND ``gate``
        (the shared human-decision gate) accepts it. Session identity, action_hash,
        state=approved, the human gate, unexpired, and single-use are ALL bound in
        one transaction; the ``consumed_at IS NULL`` compare-and-swap guarantees a
        second identical call finds nothing to consume (no replay).
        """
        self._begin()
        try:
            session = self._connection.execute(
                "SELECT id FROM sessions WHERE id = ? OR session_ref = ? "
                "ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END LIMIT 1",
                (session_id, session_id, session_id),
            ).fetchone()
            if session is None:
                self._commit()
                return None
            canonical_session_id = str(session["id"])
            rows = self._connection.execute(
                "SELECT * FROM approvals WHERE session_id = ? AND state = 'approved' "
                "AND consumed_at IS NULL ORDER BY decided_at DESC, created_at DESC, id DESC",
                (canonical_session_id,),
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                try:
                    params = json.loads(str(row.get("params_json") or "{}"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(params, dict) or params.get("action_hash") != action_hash:
                    continue
                expires_at = row.get("expires_at")
                if expires_at is not None and str(expires_at) <= now_iso:
                    continue
                if not gate(row):
                    continue
                cursor = self._connection.execute(
                    "UPDATE approvals SET consumed_at = ? "
                    "WHERE id = ? AND state = 'approved' AND consumed_at IS NULL "
                    "AND (expires_at IS NULL OR expires_at > ?)",
                    (now_iso, str(row["id"]), now_iso),
                )
                if cursor.rowcount == 1:
                    self._commit()
                    return str(row["id"])
            self._commit()
            return None
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def approval_counts(self, session_id: str) -> dict[str, int]:
        """Aggregate per-session approval totals (T-CODE-005).

        Returns approvals_requested (all approvals ever created for the session),
        approvals_granted (state=approved) and approvals_denied (state=rejected),
        grouped in a single query on the shared approvals table. Sessions with no
        approvals return zeros."""
        counts = {
            "approvals_requested": 0,
            "approvals_granted": 0,
            "approvals_denied": 0,
        }
        for row in self._connection.execute(
            "SELECT state, COUNT(*) AS n FROM approvals WHERE session_id = ? GROUP BY state",
            (session_id,),
        ).fetchall():
            n = int(row["n"])
            counts["approvals_requested"] += n
            if str(row["state"]) == ApprovalState.APPROVED.value:
                counts["approvals_granted"] += n
            elif str(row["state"]) == ApprovalState.REJECTED.value:
                counts["approvals_denied"] += n
        return counts

    @_serialized
    def get_latest_session_approval(self, session_id: str) -> dict[str, Any] | None:
        """The most recently created approval row for a session, whatever its state.

        ROW RECENCY ONLY. It cannot answer "which approval is outstanding", and
        it must not be used to pick among several: a session can hold multiple
        pending approvals for unrelated actions (this DAL dedupes only rows that
        share an ``action_hash``), and a requeued pair shares a one-second
        ``created_at`` so the ordering falls through to a comparison of two
        random hex ids. Supervision reads state via ``list_session_approvals``
        and selects by the question it is asking; this helper now has no
        production caller and is retained for tests and ad-hoc inspection.
        """
        return _row(
            self._connection.execute(
                "SELECT * FROM approvals WHERE session_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        )

    @_serialized
    def record_session_approval_delivery(
        self,
        approval_id: str,
        *,
        delivered: bool,
        attempted_at: str | None = None,
    ) -> bool:
        """Persist an attempted human-facing approval delivery.

        A later failed retry must never erase a previous successful delivery:
        once a human-facing transport accepted the alert, the pending approval
        remains ``delivered`` for max-park purposes.  ``attempted_at`` exists
        for deterministic tests; production callers use the current UTC stamp.
        """
        stamp = attempted_at or utc_now_iso()
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE approvals SET "
                "delivery_state = CASE "
                "WHEN ? THEN 'delivered' "
                "WHEN delivery_state = 'delivered' THEN 'delivered' "
                "ELSE 'failed' END, "
                "delivery_attempted_at = ?, "
                "delivered_at = CASE WHEN ? THEN ? ELSE delivered_at END "
                "WHERE id = ?",
                (int(delivered), stamp, int(delivered), stamp, approval_id),
            )
            self._commit()
            return cursor.rowcount > 0
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def max_park_extended_since(self, session_id: str) -> str | None:
        """Latest durable max-park re-arm for *session_id*, if any."""
        row = self._connection.execute(
            "SELECT ts FROM events WHERE action = 'reaper.max_park_extended' "
            "AND target_type = 'session' AND target_id = ? "
            "ORDER BY ts DESC, id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return str(row["ts"]) if row is not None else None

    @_serialized
    def max_park_extension_count(self, session_id: str) -> int:
        """Number of durable never-delivered re-arms for forensic reporting."""
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM events WHERE action = 'reaper.max_park_extended' "
            "AND target_type = 'session' AND target_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    @_serialized
    def record_max_park_extension(
        self, session_id: str, *, approval_id: str | None, detail: str
    ) -> None:
        """Durably re-arm an undelivered park, including the no-approval shape."""
        self._begin()
        try:
            sequence = _next_event_sequence(self._connection, session_id)
            self._connection.execute(
                "INSERT INTO events "
                "(ts, type, actor, action, target_type, target_id, payload_json, trace_id, "
                "execution_id, sequence) "
                "VALUES (?, 'audit', 'session-supervisor', 'reaper.max_park_extended', "
                "'session', ?, ?, '', ?, ?)",
                (
                    utc_now_iso(),
                    session_id,
                    json.dumps(
                        {"approval_id": approval_id, "detail": detail},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    session_id,
                    sequence,
                ),
            )
            self._commit()
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def list_session_approvals(self, session_id: str) -> list[dict[str, Any]]:
        return _rows(
            self._connection.execute(
                "SELECT * FROM approvals WHERE session_id = ? ORDER BY created_at ASC, id ASC",
                (session_id,),
            ).fetchall()
        )

    @_serialized
    def has_pending_approval(self, session_id: str) -> bool:
        row = self._connection.execute(
            "SELECT EXISTS("
            "SELECT 1 FROM approvals WHERE session_id = ? AND state = 'pending'"
            ") AS found",
            (session_id,),
        ).fetchone()
        return bool(row["found"])

    @_serialized
    def expire_approval(self, approval_id: str, note: str) -> bool:
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE approvals SET state = 'expired', decision_note = ?, decided_at = ? "
                "WHERE id = ? AND state = 'pending'",
                (note, utc_now_iso(), approval_id),
            )
            self._commit()
            return cursor.rowcount > 0
        except BaseException:
            self._rollback()
            raise

    def _has_cost_estimate_columns(self) -> bool:
        """Whether migration 118's estimate columns exist on this database.

        Memoized PRAGMA check rather than an assumption: a telemetry write must
        degrade to "no estimate" on a database that predates 118, never raise
        into the run that produced the usage.
        """
        if self._cost_estimate_columns is not None:
            return self._cost_estimate_columns
        try:
            names = {
                str(row["name"])
                for row in self._connection.execute("PRAGMA table_info(sessions)").fetchall()
            }
            present = {"cost_estimate_usd", "cost_estimate_source"} <= names
        except sqlite3.Error:  # pragma: no cover -- unreadable schema, stay silent
            present = False
        self._cost_estimate_columns = present
        return present

    def _token_cost_estimate(
        self,
        session_id: str,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> tuple[float, str] | None:
        """An upper-bound dollar estimate for this row's measured tokens, or None.

        None whenever the model is unknown to the model-intelligence registry or
        the registry publishes no rate for it: an unpriceable session stays
        unpriced rather than acquiring an invented number.
        """
        if input_tokens is None and output_tokens is None:
            return None
        try:
            row = self._connection.execute(
                "SELECT model FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            model = None if row is None else row["model"]
            # Local import: sessions is a lower layer than budget, and this must
            # not pay a registry/YAML import at module load.
            from omniagentos.budget.token_pricing import estimate_token_cost

            estimate = estimate_token_cost(model, input_tokens, output_tokens)
        except Exception:  # noqa: BLE001 -- pricing must never fail a telemetry write
            LOG.debug("token cost estimate unavailable for %s", session_id, exc_info=True)
            return None
        if estimate is None:
            return None
        return estimate.cost_usd, estimate.source

    @_serialized
    def record_session_usage(
        self,
        session_id: str,
        *,
        effort: str | None = None,
        cost_usd: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        wall_ms: int | None = None,
        usage_source: str | None = None,
    ) -> bool:
        """Attach measured usage to a session row (migration 049 telemetry).

        Only non-None fields are written, with one deliberate exception: when
        ``usage_source`` is the tokens-only marker (``cli-report-tokens-only``,
        see ``swarm.usage_capture.SOURCE_TOKENS_ONLY``) and ``cost_usd`` is
        None, ``cost_usd`` is written as SQL NULL. That clears any creation-time
        seed of 0.0 so an unpriced token report cannot look like a measured
        zero. An explicit ``cost_usd=0.0`` (provider said free) still writes
        0.0. Never raises on an unknown session -- losing a telemetry row is
        acceptable, failing the run that produced it is not.

        C4: when the provider reported tokens but no price, the measured tokens
        are ALSO priced into ``cost_estimate_usd`` (migration 118) from the
        model-intelligence registry's published rates. That is a second column,
        never this one: ``cost_usd`` stays NULL so "unpriced" and "genuinely
        free" remain distinguishable, while dollar-cap accrual finally has a
        number to add. An unpriceable model leaves the estimate NULL too.
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
        # Tokens without a price: force cost_usd NULL so the row cannot keep a
        # false 0.0 seed. Matches usage_capture.SOURCE_TOKENS_ONLY by value so
        # this DAL does not import the swarm package (sessions is lower layer).
        if usage_source == "cli-report-tokens-only" and cost_usd is None:
            assignments["cost_usd"] = None
        if cost_usd is None and self._has_cost_estimate_columns():
            estimate = self._token_cost_estimate(
                session_id, input_tokens=input_tokens, output_tokens=output_tokens
            )
            if estimate is not None:
                assignments["cost_estimate_usd"] = estimate[0]
                assignments["cost_estimate_source"] = estimate[1]
        if not assignments:
            return False
        clause = ", ".join(f"{key} = ?" for key in assignments)
        self._begin()
        try:
            cursor = self._connection.execute(
                f"UPDATE sessions SET {clause} WHERE id = ?",
                (*assignments.values(), session_id),
            )
            self._commit()
            return cursor.rowcount == 1
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def terminalize_session(
        self,
        session_id: str,
        target: str,
        *,
        killed_by: str | None = None,
        void_note: str,
    ) -> bool:
        """Transition to a terminal state AND void pending approvals in ONE transaction.

        T-OPS-007: there is no window in which a terminal session still holds a
        pending approval. killed_by is persisted here (durable before the manifest
        write), so a crash before the manifest cannot lose the killer identity.
        """
        wanted = SessionState(target)
        if wanted not in TERMINAL_SESSION_STATES:
            raise ValueError("terminalize requires a terminal state")
        self._begin()
        try:
            changed = self._terminalize_session_in_transaction(
                session_id,
                wanted,
                killed_by=killed_by,
                void_note=void_note,
            )
            self._commit()
            if changed:
                self._last_activity_write.pop(session_id, None)
            return changed
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def reconcile_park_or_kill(self, session_id: str, kill_target: str) -> str:
        """Atomically park on an actionable approval or terminalize the session.

        An actionable approval is pending, or approved but not yet consumed. The
        complete decision and state mutation happen under one BEGIN IMMEDIATE.
        """
        wanted = SessionState(kill_target)
        if wanted not in TERMINAL_SESSION_STATES:
            raise ValueError("reconcile kill target requires a terminal state")
        self._begin()
        try:
            row = self._connection.execute(
                "SELECT state FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                self._commit()
                return "unchanged"
            current = SessionState(str(row["state"]))
            if current in TERMINAL_SESSION_STATES or current == SessionState.AWAITING_APPROVAL:
                self._commit()
                return "unchanged"

            actionable = self._connection.execute(
                "SELECT EXISTS("
                "SELECT 1 FROM approvals WHERE session_id = ? "
                "AND (state = 'pending' OR (state = 'approved' AND consumed_at IS NULL))"
                ") AS found",
                (session_id,),
            ).fetchone()
            if bool(actionable["found"]):
                now = utc_now_iso()
                cursor = self._connection.execute(
                    "UPDATE sessions SET state = ?, updated_at = ? "
                    "WHERE id = ? AND state = ? AND state IN ('starting', 'running', 'resuming')",
                    (
                        SessionState.AWAITING_APPROVAL.value,
                        now,
                        session_id,
                        current.value,
                    ),
                )
                if cursor.rowcount != 1:
                    self._commit()
                    return "unchanged"
                self._event(session_id, SessionState.AWAITING_APPROVAL.value)
                self._commit()
                return "parked"

            changed = self._terminalize_session_in_transaction(
                session_id,
                wanted,
                killed_by="reconcile",
                void_note=f"session entered terminal state {wanted.value}",
                current=current,
            )
            self._commit()
            if not changed:
                return "unchanged"
            self._last_activity_write.pop(session_id, None)
            return "terminalized"
        except BaseException:
            self._rollback()
            raise

    def _terminalize_session_in_transaction(
        self,
        session_id: str,
        wanted: SessionState,
        *,
        killed_by: str | None,
        void_note: str,
        current: SessionState | None = None,
    ) -> bool:
        """Apply terminal state and approval expiry inside the caller's transaction.

        ``killed_by`` uses COALESCE semantics: an incoming None (natural exit)
        preserves any attribution already on the row — e.g. ``cancel_requested``
        written by ``request_cancel`` before the process exited on its own — so
        the D7 blocklist still classifies the session as operator-superseded.
        A non-None incoming value overwrites as before.
        """
        if wanted not in TERMINAL_SESSION_STATES:
            raise ValueError("terminalize requires a terminal state")
        if current is None:
            row = self._connection.execute(
                "SELECT state FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return False
            current = SessionState(str(row["state"]))
        if current in TERMINAL_SESSION_STATES or not can_transition_session(current, wanted):
            return False
        now = utc_now_iso()
        cursor = self._connection.execute(
            "UPDATE sessions SET state = ?, killed_by = COALESCE(?, killed_by), updated_at = ? "
            "WHERE id = ? AND state = ?",
            (wanted.value, killed_by, now, session_id, current.value),
        )
        if cursor.rowcount != 1:
            return False
        self._clear_attention_in_transaction(session_id)
        self._event(session_id, wanted.value)
        self._connection.execute(
            "UPDATE approvals SET state = 'expired', decision_note = ?, decided_at = ? "
            "WHERE session_id = ? AND state = 'pending'",
            (void_note, now, session_id),
        )
        return True

    @_serialized
    def enqueue_spawn(
        self,
        *,
        session_id: str,
        project_dir: str,
        model: str,
        prompt: str,
        budget_usd_max: float | None,
        title: str | None,
    ) -> str:
        """Enqueue a spawn request for the running supervisor to launch (T-OPS-003)."""
        request_id = new_id("spq")
        now = utc_now_iso()
        self._begin()
        try:
            self._connection.execute(
                "INSERT INTO session_spawn_queue "
                "(id, session_id, project_dir, model, prompt, budget_usd_max, title, "
                "state, error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', NULL, ?, ?)",
                (
                    request_id,
                    session_id,
                    project_dir,
                    model,
                    prompt,
                    budget_usd_max,
                    title,
                    now,
                    now,
                ),
            )
            self._commit()
            return request_id
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def claim_spawn_requests(self) -> list[dict[str, Any]]:
        """Return queued spawn requests (FIFO). Caller launches then marks each done."""
        return _rows(
            self._connection.execute(
                "SELECT * FROM session_spawn_queue WHERE state = 'queued' "
                "ORDER BY created_at ASC, id ASC"
            ).fetchall()
        )

    @_serialized
    def has_queued_spawn(self, session_id: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM session_spawn_queue WHERE session_id = ? AND state = 'queued' LIMIT 1",
                (session_id,),
            ).fetchone()
            is not None
        )

    @_serialized
    def mark_spawn_result(self, request_id: str, state: str, error: str | None = None) -> bool:
        if state not in {"launched", "failed"}:
            raise ValueError("spawn result must be launched or failed")
        return self._update_spawn(request_id, state, error)

    def _update_spawn(self, request_id: str, state: str, error: str | None) -> bool:
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE session_spawn_queue SET state = ?, error = ?, updated_at = ? "
                "WHERE id = ? AND state = 'queued'",
                (state, error, utc_now_iso(), request_id),
            )
            self._commit()
            return cursor.rowcount > 0
        except BaseException:
            self._rollback()
            raise

    @_serialized
    def void_session_approvals(self, session_id: str, note: str) -> int:
        self._begin()
        try:
            cursor = self._connection.execute(
                "UPDATE approvals SET state = 'expired', decision_note = ?, decided_at = ? "
                "WHERE session_id = ? AND state = 'pending'",
                (note, utc_now_iso(), session_id),
            )
            self._commit()
            return cursor.rowcount
        except BaseException:
            self._rollback()
            raise


__all__ = [
    "SESSION_TRANSITIONS",
    "TERMINAL_SESSION_STATES",
    "SessionState",
    "SessionsDal",
    "can_transition_session",
]
