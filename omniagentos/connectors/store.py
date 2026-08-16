"""Persistence for capability grants (migration 005).

Composes the H1 :class:`SqliteStore` connection, matching ``collab/store.py``.

Every write validates the capability id against the connector registry before it
touches the database. An unknown capability is refused rather than stored: a grant
row whose meaning cannot be resolved is worse than no row at all, because the UI
would render it as access the operator believes is real.

Grants are recorded twice on purpose -- ``agent_capabilities`` holds current state
and is mutated; ``capability_grant_log`` is append-only, so "who gave this agent
the ability to move an ad budget, and when?" survives a later revoke.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, cast

from omniagentos.connectors import ConnectorError, load_registry
from omniagentos.contracts import utc_now_iso
from omniagentos.db.busy import BusyRetryPolicy, run_with_busy_retry
from omniagentos.db.store import SqliteStore

_AUDIT_BUSY_RETRY = BusyRetryPolicy(
    max_attempts=10,
    base_delay_ms=10,
    max_delay_ms=400,
    jitter_ms=0,
)


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Serialize access through the composed H1 store's connection lock."""

    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        store = cast("CapabilityStore", args[0])
        with store._store._lock:
            return method(*args, **kwargs)

    return wrapped


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _validate_expiry(expires_at: str | None) -> None:
    """Ensure an optional expiry is an offset-aware ISO 8601 timestamp."""
    if expires_at is None:
        return
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ConnectorError("expires_at must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ConnectorError("expires_at must include a UTC offset")


class CapabilityStore:
    """Read and write agent capability grants."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._store._connection

    @staticmethod
    def _validate(capability_ids: Iterable[str]) -> dict[str, str]:
        """Reject unknown capabilities; return {cap_id: action_class} for the rest.

        Fails on the FIRST unknown id rather than silently dropping it, so a typo in
        a grant request surfaces as an error instead of as an agent quietly holding
        less access than the operator thinks it does.
        """
        registry = load_registry()
        classes: dict[str, str] = {}
        for cap_id in capability_ids:
            try:
                cap = registry.capability(cap_id)
            except ConnectorError as exc:
                raise ConnectorError(f"Refusing to grant unknown capability {cap_id!r}") from exc
            classes[cap_id] = cap.action_class.value
        return classes

    @_serialized
    def get_grant(self, agent_id: str) -> list[str]:
        """Capability ids currently held by one agent."""
        rows = self._conn.execute(
            "SELECT capability_id FROM agent_capabilities WHERE agent_id = ? "
            "ORDER BY capability_id",
            (agent_id,),
        ).fetchall()
        return [str(row["capability_id"]) for row in rows]

    @_serialized
    def grants_for_all(self) -> dict[str, list[str]]:
        """Every agent's grant, keyed by agent id. One query, for the roster view."""
        rows = self._conn.execute(
            "SELECT agent_id, capability_id FROM agent_capabilities "
            "ORDER BY agent_id, capability_id"
        ).fetchall()
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(str(row["agent_id"]), []).append(str(row["capability_id"]))
        return out

    @_serialized
    def holders_of(self, capability_id: str) -> list[str]:
        """Agent ids holding one capability -- 'who can refund a customer?'."""
        rows = self._conn.execute(
            "SELECT agent_id FROM agent_capabilities WHERE capability_id = ? ORDER BY agent_id",
            (capability_id,),
        ).fetchall()
        return [str(row["agent_id"]) for row in rows]

    @_serialized
    def set_grant(
        self,
        agent_id: str,
        capability_ids: list[str],
        *,
        actor: str = "operator",
        note: str = "",
    ) -> list[str]:
        """Replace an agent's grant wholesale, logging every add and remove.

        Validation happens BEFORE the transaction opens, so a request containing one
        bad capability id leaves the existing grant untouched rather than clearing it.
        """
        desired = set(capability_ids)
        classes = self._validate(desired)

        current = set(
            str(row["capability_id"])
            for row in self._conn.execute(
                "SELECT capability_id FROM agent_capabilities WHERE agent_id = ?",
                (agent_id,),
            ).fetchall()
        )
        added = desired - current
        removed = current - desired
        now = utc_now_iso()

        self._store._begin()
        try:
            for cap_id in sorted(removed):
                self._conn.execute(
                    "DELETE FROM agent_capabilities WHERE agent_id = ? AND capability_id = ?",
                    (agent_id, cap_id),
                )
                self._conn.execute(
                    "INSERT INTO capability_grant_log "
                    "(agent_id, capability_id, action, action_class, actor, note, ts) "
                    "VALUES (?, ?, 'revoke', ?, ?, ?, ?)",
                    (agent_id, cap_id, classes.get(cap_id, ""), actor, note, now),
                )
            for cap_id in sorted(added):
                self._conn.execute(
                    "INSERT INTO agent_capabilities "
                    "(agent_id, capability_id, granted_at, granted_by, note) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (agent_id, cap_id, now, actor, note),
                )
                self._conn.execute(
                    "INSERT INTO capability_grant_log "
                    "(agent_id, capability_id, action, action_class, actor, note, ts) "
                    "VALUES (?, ?, 'grant', ?, ?, ?, ?)",
                    (agent_id, cap_id, classes[cap_id], actor, note, now),
                )
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise

        return sorted(desired)

    @_serialized
    def issue_scoped_grant(
        self,
        agent_id: str,
        capability_ids: list[str],
        *,
        mode: str = "read",
        expires_at: str | None = None,
        issued_by: str = "operator",
        request_id: str | None = None,
        project_id: str | None = None,
        actor: str = "operator",
        note: str = "",
    ) -> list[str]:
        """Issue scoped capability grants without replacing an agent's other grants.

        A current-state row has one scope per ``(agent_id, capability_id)``.  Reissuing
        that capability refreshes its lifecycle metadata while other capability rows
        remain intact; each issuance is still retained in the append-only audit log.

        ``project_id`` (C11/D-31, migration 115) binds the grant to ONE project.
        Omitting it leaves the row unbound, and an unbound row does not authorize
        project-scoped work at all — the broker refuses it rather than treating
        "no project stated" as "every project". Because the current-state row is
        keyed ``(agent_id, capability_id)``, reissuing with a different project
        REBINDS the grant instead of adding a second one: an agent holds a
        capability in one project at a time, and the move is recorded in the
        append-only log.
        """
        desired = sorted(set(capability_ids))
        classes = self._validate(desired)
        if mode not in {"read", "write"}:
            raise ConnectorError("mode must be 'read' or 'write'")
        _validate_expiry(expires_at)

        now = utc_now_iso()
        self._store._begin()
        try:
            for cap_id in desired:
                self._conn.execute(
                    "INSERT INTO agent_capabilities "
                    "(agent_id, capability_id, granted_at, granted_by, note, mode, expires_at, "
                    "issued_by, request_id, project_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(agent_id, capability_id) DO UPDATE SET "
                    "granted_at = excluded.granted_at, granted_by = excluded.granted_by, "
                    "note = excluded.note, mode = excluded.mode, expires_at = excluded.expires_at, "
                    "issued_by = excluded.issued_by, request_id = excluded.request_id, "
                    "project_id = excluded.project_id",
                    (
                        agent_id,
                        cap_id,
                        now,
                        actor,
                        note,
                        mode,
                        expires_at,
                        issued_by,
                        request_id,
                        project_id,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO capability_grant_log "
                    "(agent_id, capability_id, action, action_class, actor, note, ts, mode, "
                    "expires_at, issued_by, request_id) "
                    "VALUES (?, ?, 'grant', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        agent_id,
                        cap_id,
                        classes[cap_id],
                        actor,
                        note,
                        now,
                        mode,
                        expires_at,
                        issued_by,
                        request_id,
                    ),
                )
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise
        return desired

    @_serialized
    def get_grant_row(self, agent_id: str, capability_id: str) -> dict[str, Any] | None:
        """Return one current grant with its lifecycle metadata, if it exists."""
        row = self._conn.execute(
            "SELECT * FROM agent_capabilities WHERE agent_id = ? AND capability_id = ?",
            (agent_id, capability_id),
        ).fetchone()
        return dict(row) if row is not None else None

    @_serialized
    def grant_log(self, agent_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Append-only history of grant changes, newest first."""
        if agent_id:
            rows = self._conn.execute(
                "SELECT * FROM capability_grant_log WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
                (agent_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM capability_grant_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return _rows(rows)

    # --- broker tokens: how a run proves which agent it is ---

    @_serialized
    def issue_token(self, run_id: str, agent_id: str, ttl_seconds: int = 6 * 3600) -> str:
        """Mint an opaque per-run bearer token.

        The token NAMES the agent; it does not describe its powers. The grant is
        resolved from the database at call time, so an agent that reads (or edits)
        its own environment gains nothing -- there is no grant in there to widen.
        """
        import secrets
        from datetime import datetime, timedelta

        token = secrets.token_urlsafe(32)
        now = datetime.now(tz=UTC)
        self._store._begin()
        try:
            self._conn.execute(
                "INSERT INTO broker_tokens (token, run_id, agent_id, issued_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    token,
                    run_id,
                    agent_id,
                    now.isoformat(),
                    (now + timedelta(seconds=ttl_seconds)).isoformat(),
                ),
            )
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise
        return token

    @_serialized
    def resolve_token(self, token: str) -> dict[str, Any] | None:
        """Return {run_id, agent_id} for a live token, or None. Fails closed."""
        from datetime import datetime

        row = self._conn.execute(
            "SELECT run_id, agent_id, expires_at FROM broker_tokens "
            "WHERE token = ? AND revoked = 0",
            (token,),
        ).fetchone()
        if row is None:
            return None
        try:
            if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now(tz=UTC):
                return None
        except ValueError:
            return None  # unparseable expiry -> treat as expired
        return {"run_id": str(row["run_id"]), "agent_id": str(row["agent_id"])}

    @_serialized
    def revoke_tokens_for_run(self, run_id: str) -> None:
        """Burn a run's tokens when it finishes. Access should not outlive the work."""
        self._store._begin()
        try:
            self._conn.execute("UPDATE broker_tokens SET revoked = 1 WHERE run_id = ?", (run_id,))
            self._store._commit()
        except BaseException:
            self._store._rollback()
            raise

    def log_call(
        self,
        run_id: str,
        agent_id: str,
        capability_id: str,
        *,
        method: str = "",
        path: str = "",
        allowed: bool,
        reason: str = "",
        status: int | None = None,
        call_id: str = "",
        request_id: str = "",
        task_id: str = "",
        session_id: str = "",
        holder: str = "",
        action_mode: str = "",
        connector: str = "",
        env_name: str = "",
        credential_id: str = "",
        key_version_id: str = "",
        grant_id: str = "",
        grant_issuer: str = "",
        grant_expires_at: str = "",
        registry_digest: str = "",
        target_host: str = "",
        request_schema_hash: str = "",
        decision: str = "",
        reason_code: str = "",
        upstream_status_class: str = "",
        latency_ms: int = 0,
        response_size_class: str = "",
        redaction_count: int = 0,
        budget_receipt_id: str = "",
        correlation_id: str = "",
    ) -> None:
        """Append one metadata-only audit row after a bounded lock retry.

        The parameter surface is intentionally closed over S3-W4 metadata.  It
        has no place for headers, query values, request/response bodies, or
        exception payloads.  SQLite's normal five-second busy handler is
        disabled only while attempting this append; the explicit backoff is
        bounded to approximately two seconds so credential use can fail closed
        promptly when the durable intent cannot be written.
        """
        row_reason = reason_code or reason

        def append() -> None:
            with self._store._lock:
                connection = self._conn
                connection.execute("PRAGMA busy_timeout=0")
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                "INSERT INTO broker_calls "
                        "(run_id, agent_id, capability_id, method, path, allowed, reason, status, "
                        "ts, call_id, request_id, task_id, session_id, holder, action_mode, "
                        "connector, env_name, credential_id, key_version_id, grant_id, "
                        "grant_issuer, grant_expires_at, registry_digest, target_host, "
                        "request_schema_hash, decision, reason_code, upstream_status_class, "
                        "latency_ms, response_size_class, redaction_count, budget_receipt_id, "
                        "correlation_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            agent_id,
                            capability_id,
                            method,
                            path,
                            1 if allowed else 0,
                            row_reason,
                            status,
                            utc_now_iso(),
                            call_id,
                            request_id,
                            task_id,
                            session_id,
                            holder or agent_id,
                            action_mode,
                            connector,
                            env_name,
                            credential_id,
                            key_version_id,
                            grant_id,
                            grant_issuer,
                            grant_expires_at,
                            registry_digest,
                            target_host,
                            request_schema_hash,
                            decision,
                            row_reason,
                            upstream_status_class,
                            max(0, int(latency_ms)),
                            response_size_class,
                            max(0, int(redaction_count)),
                            budget_receipt_id,
                            correlation_id,
                        ),
                    )
                    connection.commit()
                    self._store.maybe_checkpoint_wal()
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
                finally:
                    connection.execute("PRAGMA busy_timeout=5000")

        run_with_busy_retry(append, policy=_AUDIT_BUSY_RETRY, op="broker_audit_append")

    @_serialized
    def call_log(self, run_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """What the agents actually did -- the after-the-fact view of an overnight loop."""
        if run_id:
            rows = self._conn.execute(
                "SELECT * FROM broker_calls WHERE run_id = ? ORDER BY id DESC LIMIT ?",
                (run_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM broker_calls ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return _rows(rows)


__all__ = ["CapabilityStore"]
