"""Live SQLite implementation of the memory conversation reads + turn appends.

Composed over an already-configured :class:`SqliteStore` (the same pattern as
:class:`omniagentos.projects.store.ProjectStore`) so every read/write serializes on the
one connection lock. All access to the FROZEN ``conversations`` table (migration 031,
owned by W3-hierarchy) and to ``projects.parent_project_id`` degrades to an empty result
when the table/column is not yet present, so this store is safe to use before 031 lands.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from functools import wraps
from typing import Any, cast

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.memory.contracts import ConversationTurn, ScopeRef, ScopeType

# A task cannot be its own great-grandparent, but a corrupt parent_project_id cycle must
# never spin the ancestor walk; bound the depth defensively.
_MAX_ANCESTOR_DEPTH = 32


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        store = cast("ConversationStore", args[0])
        with store._store._lock:
            return method(*args, **kwargs)

    return wrapped


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


class ConversationStore:
    """Frozen ``conversations`` reads/writes + hierarchy ancestor resolution."""

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

    # --- reads (ConversationReader protocol) --------------------------------

    @_serialized
    def recent_turns(self, scope_type: str, scope_id: str, limit: int) -> list[ConversationTurn]:
        if limit <= 0:
            return []
        try:
            rows = self._connection.execute(
                "SELECT seq, role, content, model, created_at, meta_json "
                "FROM conversations WHERE scope_type = ? AND scope_id = ? "
                "ORDER BY seq DESC LIMIT ?",
                (scope_type, scope_id, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # Table absent (pre-031) or otherwise unreadable — no history to inject.
            return []
        turns = [
            ConversationTurn(
                seq=int(row["seq"]),
                role=str(row["role"]),  # type: ignore[arg-type]
                content=str(row["content"] or ""),
                model=row["model"],
                created_at=row["created_at"],
                meta=_parse_meta(row["meta_json"]),
            )
            for row in rows
        ]
        # Query is newest-first for the LIMIT; return ascending-seq (chronological).
        turns.reverse()
        return turns

    @_serialized
    def resolve_ancestors(self, scope_type: str, scope_id: str) -> list[ScopeRef]:
        if scope_type == "task":
            task = self._store.get_task(scope_id)
            project_id = None if task is None else task.get("project_id")
            if not project_id:
                return []
            # The task's immediate parent is its project (inclusive), then the project
            # ancestry above it.
            return self._project_chain(str(project_id), include_self=True)
        if scope_type == "project":
            return self._project_chain(scope_id, include_self=False)
        return []

    @_serialized
    def rolling_summary(self, scope_type: str, scope_id: str) -> str | None:
        return self._rolling_summary_unlocked(scope_type, scope_id)

    # --- writes -------------------------------------------------------------

    @_serialized
    def append_turn(
        self,
        scope_type: str,
        scope_id: str,
        role: str,
        content: str,
        *,
        model: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ConversationTurn | None:
        """Append a turn with the next per-scope ``seq``; return it (or ``None`` if the
        table is absent). Seq allocation + insert share one transaction so concurrent
        appends cannot collide on UNIQUE(scope_type, scope_id, seq)."""
        meta_json = json.dumps(meta or {}, separators=(",", ":"), sort_keys=True, default=str)
        now = utc_now_iso()
        turn_id = new_id("cnv")
        self._store._begin()
        try:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq FROM conversations "
                "WHERE scope_type = ? AND scope_id = ?",
                (scope_type, scope_id),
            ).fetchone()
            seq = int(row["next_seq"]) if row is not None else 0
            self._connection.execute(
                "INSERT INTO conversations "
                "(id, scope_type, scope_id, seq, role, content, model, created_at, meta_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (turn_id, scope_type, scope_id, seq, role, content, model, now, meta_json),
            )
            self._store._commit()
        except sqlite3.OperationalError:
            # Table absent (pre-031): persistence is a no-op, not a failure.
            self._store._rollback()
            return None
        except BaseException:
            self._store._rollback()
            raise
        return ConversationTurn(
            seq=seq,
            role=cast(Any, role),
            content=content,
            model=model,
            created_at=now,
            meta=dict(meta or {}),
        )

    @_serialized
    def purge_turns_by_meta(
        self,
        scope_type: str,
        scope_id: str,
        *,
        kind: str,
        keep: int,
    ) -> int:
        """Delete older turns of one metadata kind, retaining newest ``keep``.

        This is the bounded-growth seam used by high-frequency lifecycle
        capture. It shares the store lock and transaction discipline with
        ``append_turn`` so purge cannot interleave with sequence allocation.
        """
        keep = max(1, int(keep))
        self._store._begin()
        try:
            cursor = self._connection.execute(
                """
                DELETE FROM conversations
                WHERE id IN (
                    SELECT id FROM conversations
                    WHERE scope_type = ?
                      AND scope_id = ?
                      AND json_extract(meta_json, '$.kind') = ?
                    ORDER BY seq DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (scope_type, scope_id, kind, keep),
            )
            self._store._commit()
            return max(0, int(cursor.rowcount))
        except sqlite3.OperationalError:
            self._store._rollback()
            return 0
        except BaseException:
            self._store._rollback()
            raise

    # --- internals (assume the store lock is held) --------------------------

    def _project_chain(self, project_id: str, *, include_self: bool) -> list[ScopeRef]:
        chain: list[str] = []
        seen: set[str] = set()
        current: str | None = project_id if include_self else self._parent_of(project_id)
        while current and current not in seen and len(chain) < _MAX_ANCESTOR_DEPTH:
            seen.add(current)
            chain.append(current)
            current = self._parent_of(current)
        chain.reverse()  # root first, immediate parent last
        scope: ScopeType = "project"
        return [ScopeRef(scope, pid) for pid in chain]

    def _parent_of(self, project_id: str) -> str | None:
        try:
            row = self._connection.execute(
                "SELECT parent_project_id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            # projects table missing, or parent_project_id column absent (pre-031).
            return None
        if row is None:
            return None
        parent = row["parent_project_id"]
        return str(parent) if parent else None

    def _rolling_summary_unlocked(self, scope_type: str, scope_id: str) -> str | None:
        try:
            # Prefer an explicit stored summary turn (role=system, meta.kind='summary').
            row = self._connection.execute(
                "SELECT content FROM conversations "
                "WHERE scope_type = ? AND scope_id = ? AND role = 'system' "
                "AND json_extract(meta_json, '$.kind') = 'summary' "
                "ORDER BY seq DESC LIMIT 1",
                (scope_type, scope_id),
            ).fetchone()
            if row is not None and str(row["content"] or "").strip():
                return str(row["content"])
            # Fall back to the FIRST user turn — the original brief for this node, which
            # is exactly the "what is this about" a downstream agent should inherit.
            first = self._connection.execute(
                "SELECT content FROM conversations "
                "WHERE scope_type = ? AND scope_id = ? AND role = 'user' "
                "ORDER BY seq ASC LIMIT 1",
                (scope_type, scope_id),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if first is not None and str(first["content"] or "").strip():
            return str(first["content"])
        return None


__all__ = ["ConversationStore"]
