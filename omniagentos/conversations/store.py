"""Thread-safe DAL for W3 scoped conversations (project & task message logs).

Composed over the shared :class:`~omniagentos.db.store.SqliteStore` connection
(same pattern as :class:`~omniagentos.projects.store.ProjectStore`) so appends
serialize against the one connection lock. A conversation is just an ordered log
keyed by ``(scope_type, scope_id)``; ``seq`` is assigned as ``MAX(seq)+1`` for
that scope inside the append transaction, so concurrent appends can never collide
on the ``UNIQUE(scope_type, scope_id, seq)`` constraint.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore

SCOPE_TYPES = frozenset({"project", "task", "chat"})
ROLES = frozenset({"user", "agent", "system"})


class ConversationError(ValueError):
    """Raised when a conversation message cannot be persisted safely."""


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _parse_meta(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _message(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["meta"] = _parse_meta(out.pop("meta_json", None))
    return out


class ConversationStore:
    """Append/read DAL for scope-keyed conversation logs."""

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

    def append(
        self,
        scope_type: str,
        scope_id: str,
        role: str,
        content: str,
        *,
        model: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a message to a scope's log and return the stored row.

        ``seq`` is assigned atomically as the next value for the scope; the whole
        insert runs under the store lock so parallel appends stay ordered.
        """
        if scope_type not in SCOPE_TYPES:
            raise ConversationError(f"invalid scope_type {scope_type!r}")
        if role not in ROLES:
            raise ConversationError(f"invalid role {role!r}")
        message_id = new_id("cnv")
        now = utc_now_iso()
        meta_json = _json(meta or {})
        with self._store._lock:
            self._store._begin()
            try:
                row = self._connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM conversations "
                    "WHERE scope_type = ? AND scope_id = ?",
                    (scope_type, scope_id),
                ).fetchone()
                seq = int(row["next_seq"])
                self._connection.execute(
                    "INSERT INTO conversations "
                    "(id, scope_type, scope_id, seq, role, content, model, "
                    "created_at, meta_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        message_id,
                        scope_type,
                        scope_id,
                        seq,
                        role,
                        content,
                        model,
                        now,
                        meta_json,
                    ),
                )
                self._store._commit()
            except BaseException:
                self._store._rollback()
                raise
        return {
            "id": message_id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "seq": seq,
            "role": role,
            "content": content,
            "model": model,
            "created_at": now,
            "meta": _parse_meta(meta_json),
        }

    def read(self, scope_type: str, scope_id: str) -> list[dict[str, Any]]:
        """Return a scope's messages in ascending ``seq`` order."""
        if scope_type not in SCOPE_TYPES:
            raise ConversationError(f"invalid scope_type {scope_type!r}")
        with self._store._lock:
            rows = self._connection.execute(
                "SELECT * FROM conversations WHERE scope_type = ? AND scope_id = ? "
                "ORDER BY seq ASC",
                (scope_type, scope_id),
            ).fetchall()
        return [_message(dict(row)) for row in rows]


__all__ = ["ROLES", "SCOPE_TYPES", "ConversationError", "ConversationStore"]
