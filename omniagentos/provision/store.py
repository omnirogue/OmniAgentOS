"""Persistence for provisioning: scope-request audit + effective-scope mutation.

Composed over an already-configured :class:`SqliteStore` (the same pattern as
:class:`omniagentos.projects.store.ProjectStore`), so every write serializes on
the one connection lock rather than opening a second connection.

Two responsibilities:

* record every request-more-scope decision (auto-granted or escalated) in
  ``project_scope_requests`` (migration 023) -- the decision log the UI reads; and
* widen a project's *effective* scope on an auto-grant by appending a dir to
  ``projects.root_dirs_json`` / ``allowed_dirs_json`` or a connector capability to
  ``allowed_tools_json`` (migration 014). The projects tables stay the source of
  truth for what a project may touch; this module only ever *adds*, never relaxes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from functools import wraps
from typing import Any, cast

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        store = cast("ProvisionStore", args[0])
        with store._store._lock:
            return method(*args, **kwargs)

    return wrapped


def _load_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _dump_list(value: list[str]) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


class ProvisionError(ValueError):
    """Raised when a scope request references an unknown project."""


class ProvisionStore:
    """Scope-request log + effective-scope widening over a shared SqliteStore."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @property
    def _connection(self) -> sqlite3.Connection:
        """The CALLING thread's connection, resolved live from the composed store.

        Never cache this on the instance. ``SqliteStore`` hands out one
        connection per thread and opens them with ``check_same_thread=False``,
        so a handle captured at construction time would bind this DAL to
        whichever thread built it and silently interleave its statements into
        another thread's transaction rather than raising.
        """
        return self._store._connection

    @_serialized
    def record_request(
        self,
        project_id: str,
        *,
        scope_kind: str,
        scope_value: str,
        decision: str,
        status: str,
        action_class: str | None = None,
        reason: str = "",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = new_id("psr")
        now = utc_now_iso()
        self._store._write(
            "INSERT INTO project_scope_requests "
            "(id, project_id, run_id, scope_kind, scope_value, action_class, "
            "decision, status, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                project_id,
                run_id,
                scope_kind,
                scope_value,
                action_class,
                decision,
                status,
                reason,
                now,
            ),
        )
        return {
            "id": request_id,
            "project_id": project_id,
            "run_id": run_id,
            "scope_kind": scope_kind,
            "scope_value": scope_value,
            "action_class": action_class,
            "decision": decision,
            "status": status,
            "reason": reason,
            "created_at": now,
        }

    @_serialized
    def list_requests(self, project_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM project_scope_requests WHERE project_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (project_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @_serialized
    def grant_dir(self, project_id: str, path: str) -> list[str]:
        """Append ``path`` to the project's root_dirs and allowed_dirs (idempotent).

        Returns the new root_dirs. Raises :class:`ProvisionError` for an unknown
        project so an auto-grant can never silently no-op.
        """
        row = self._project_row(project_id)
        roots = _load_list(row["root_dirs_json"])
        allowed = _load_list(row["allowed_dirs_json"])
        if path not in roots:
            roots.append(path)
        if path not in allowed:
            allowed.append(path)
        self._store._write(
            "UPDATE projects SET root_dirs_json = ?, allowed_dirs_json = ? WHERE id = ?",
            (_dump_list(roots), _dump_list(allowed), project_id),
        )
        return roots

    @_serialized
    def grant_connector(self, project_id: str, capability_id: str) -> list[str]:
        """Append ``capability_id`` to the project's allowed_tools (idempotent)."""
        row = self._project_row(project_id)
        tools = _load_list(row["allowed_tools_json"])
        if capability_id not in tools:
            tools.append(capability_id)
        self._store._write(
            "UPDATE projects SET allowed_tools_json = ? WHERE id = ?",
            (_dump_list(tools), project_id),
        )
        return tools

    # --- internals (assume the store lock is already held) ---

    def _project_row(self, project_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT root_dirs_json, allowed_dirs_json, allowed_tools_json "
            "FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise ProvisionError(f"unknown project {project_id!r}")
        return row


__all__ = ["ProvisionError", "ProvisionStore"]
