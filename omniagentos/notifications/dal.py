"""Thread-safe SQLite access for the notifications feed.

Mirrors :class:`omniagentos.sessions.dal.SessionsDal`: a path owns its
connection (auto-migrated on construction), while a supplied ``sqlite3.Connection``
is shared and remains caller-owned. Sharing a connection lets a source that
already writes to a specific database (a test's tmp store, the supervisor's
sessions connection) persist its notification to the SAME database, so writes
stay isolated with no split-brain.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any, Literal, cast

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import _checked_fields, _connect, _insert_sql, _row, _rows

_NOTIFICATION_COLUMNS = frozenset(
    {
        "id",
        "kind",
        "title",
        "body",
        "severity",
        "ref_type",
        "ref_id",
        "created_at",
        "read_at",
        "acted_at",
        "payload_json",
    }
)

_VALID_KINDS = frozenset(
    {"approval", "alert", "escalation", "done", "blocked", "info", "swarm_failed"}
)


class NotificationPersistenceGuardRejected(RuntimeError):
    """A guarded notification write was refused before dedupe/insert.

    ``cause`` is kept intact so the service layer can re-raise the authoritative
    fencing exception (for reliability callers this is ``LeaseConflict``) rather
    than downgrading claim loss into an ordinary best-effort notification failure.
    """

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


def _serialized[**P, R](method):  # type: ignore[no-untyped-def]
    """Serialize access to the connection owned by the first argument."""

    @wraps(method)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        dal = cast("NotificationsDal", args[0])
        with dal._lock:
            return method(*args, **kwargs)

    return wrapped


class NotificationsDal:
    """Auto-migrated, serialized access to the ``notifications`` table."""

    def __init__(self, database: str | Path | sqlite3.Connection) -> None:
        self._lock = RLock()
        if isinstance(database, sqlite3.Connection):
            self._connection = database
            self._connection.row_factory = sqlite3.Row
            self._owns_connection = False
            from omniagentos.db.migrate import migrate_connection

            migrate_connection(self._connection)
        else:
            self._connection = _connect(str(database))
            self._owns_connection = True
            try:
                from omniagentos.db.migrate import migrate_connection

                migrate_connection(self._connection)
            except BaseException:
                self._connection.close()
                raise

    @_serialized
    def close(self) -> None:
        if self._owns_connection:
            self._connection.close()
            self._owns_connection = False

    @staticmethod
    def _create_values(row: dict[str, Any]) -> dict[str, Any]:
        """Normalize and validate values shared by guarded and legacy creates."""
        values = dict(row)
        values.setdefault("id", new_id("ntf"))
        values.setdefault("created_at", utc_now_iso())
        values.setdefault("body", "")
        values.setdefault("severity", "info")
        payload = values.pop("payload", None)
        if payload is not None and "payload_json" not in values:
            values["payload_json"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        values.setdefault("payload_json", "{}")
        if not str(values.get("id", "")).startswith("ntf_"):
            raise ValueError("notification id must use the ntf_ prefix")
        if str(values.get("kind")) not in _VALID_KINDS:
            raise ValueError(f"invalid notification kind {values.get('kind')!r}")
        return _checked_fields(values, _NOTIFICATION_COLUMNS)

    def _stored(self, notification_id: str) -> dict[str, Any]:
        stored = _row(
            self._connection.execute(
                "SELECT * FROM notifications WHERE id = ?", (notification_id,)
            ).fetchone()
        )
        assert stored is not None
        return stored

    @_serialized
    def create(self, row: dict[str, Any]) -> dict[str, Any]:
        """Insert one notification, returning the stored row.

        ``id`` (``ntf_`` prefix) and ``created_at`` are minted when absent; a
        dict/list ``payload`` is JSON-encoded into ``payload_json``.

        All fallible result construction happens before ``COMMIT``. Once commit
        returns, the method only returns the already-built result; a
        post-commit read can therefore never turn a landed row into a reported
        persistence failure.
        """
        checked = self._create_values(row)
        sql, parameters = _insert_sql("notifications", checked)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(sql, parameters)
            stored = self._stored(str(checked["id"]))
            self._connection.commit()
        except BaseException:
            try:
                self._connection.rollback()
            except BaseException:
                # Preserve the authoritative begin/insert/fetch/commit failure.
                # A secondary rollback error must never replace its cause.
                pass
            raise
        return stored

    @_serialized
    def create_guarded(
        self,
        row: dict[str, Any],
        *,
        guard: Callable[[sqlite3.Connection], None],
        dedupe_ref: tuple[str, str] | None = None,
    ) -> tuple[Literal["persisted", "deduped"], dict[str, Any] | None]:
        """Atomically validate a guard, dedupe, and insert one notification.

        ``BEGIN IMMEDIATE`` is acquired before ``guard`` runs and remains held
        through the optional dedupe query and insert commit. A competing lease
        owner therefore cannot replace the validated fencing token between the
        proof and the notification mutation.

        Guard rejection is wrapped separately from ordinary persistence failure
        so reliability orchestration can propagate claim loss fail-closed.
        """
        checked = self._create_values(row)
        sql, parameters = _insert_sql("notifications", checked)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                guard(self._connection)
            except BaseException as exc:
                raise NotificationPersistenceGuardRejected(exc) from exc
            if dedupe_ref is not None:
                ref_type, ref_id = dedupe_ref
                existing = self._connection.execute(
                    "SELECT 1 FROM notifications "
                    "WHERE ref_type = ? AND ref_id = ? AND read_at IS NULL LIMIT 1",
                    (ref_type, ref_id),
                ).fetchone()
                if existing is not None:
                    result: tuple[Literal["persisted", "deduped"], dict[str, Any] | None] = (
                        "deduped",
                        None,
                    )
                    self._connection.commit()
                    return result
            self._connection.execute(sql, parameters)
            # Fetch and convert while rollback is still possible. The returned
            # value is complete before COMMIT, so no read/result construction
            # can contradict a successfully landed insert.
            stored = self._stored(str(checked["id"]))
            result = ("persisted", stored)
            self._connection.commit()
        except BaseException:
            try:
                self._connection.rollback()
            except BaseException:
                # Never mask the original guard, persistence, or commit error.
                pass
            raise
        return result

    @_serialized
    def get(self, notification_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute(
                "SELECT * FROM notifications WHERE id = ?", (notification_id,)
            ).fetchone()
        )

    @_serialized
    def list(self, *, unread_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        """Newest-first notifications; ``unread_only`` restricts to unread rows."""
        if limit < 1:
            return []
        if unread_only:
            return _rows(
                self._connection.execute(
                    "SELECT * FROM notifications WHERE read_at IS NULL "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            )
        return _rows(
            self._connection.execute(
                "SELECT * FROM notifications ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        )

    @_serialized
    def unread_count(self, *, kinds: Sequence[str] | None = None) -> int:
        """Unread rows, optionally restricted to ``kinds``.

        ``kinds=None`` counts every unread row (the legacy bell badge).
        An EMPTY sequence counts nothing — "restrict to no kinds" is a real
        restriction, never a silent widening back to "all".
        """
        if kinds is None:
            row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE read_at IS NULL"
            ).fetchone()
            return int(row["n"]) if row is not None else 0
        selected = [str(kind) for kind in kinds]
        if not selected:
            return 0
        placeholders = ", ".join("?" for _ in selected)
        row = self._connection.execute(
            "SELECT COUNT(*) AS n FROM notifications "
            f"WHERE read_at IS NULL AND kind IN ({placeholders})",
            selected,
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    @_serialized
    def has_unresolved_for_ref(self, ref_type: str, ref_id: str) -> bool:
        """True iff an UNREAD notification already targets this entity.

        The idempotency guard used by every source: a source that re-fires for
        the same approval/session (a retried hook eval, a re-parked session)
        must not spawn a duplicate notification while the first is still unread.
        """
        row = self._connection.execute(
            "SELECT 1 FROM notifications "
            "WHERE ref_type = ? AND ref_id = ? AND read_at IS NULL LIMIT 1",
            (ref_type, ref_id),
        ).fetchone()
        return row is not None

    @_serialized
    def has_for_ref_kind(self, ref_type: str, ref_id: str, kind: str) -> bool:
        """True iff ANY notification (read or unread) of ``kind`` targets this entity.

        The idempotency guard for terminal, kind-specific events like task
        completion: unlike :meth:`has_unresolved_for_ref` (which only looks at
        UNREAD rows, for re-firing live sources), a ``done`` notification must
        never be re-emitted even after the operator has read it -- two intake
        emit points (the orchestration lifecycle and board reconcile) can both
        observe the same completion, so this makes the second a no-op. The
        ``idx_notifications_ref`` index (``ref_type, ref_id``) covers the lookup.
        """
        row = self._connection.execute(
            "SELECT 1 FROM notifications WHERE ref_type = ? AND ref_id = ? AND kind = ? LIMIT 1",
            (ref_type, ref_id, kind),
        ).fetchone()
        return row is not None

    @_serialized
    def mark_read(self, notification_id: str) -> dict[str, Any] | None:
        """Stamp ``read_at`` (idempotent) and return the row, or None if absent."""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
                (utc_now_iso(), notification_id),
            )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        return self.get(notification_id)

    @_serialized
    def mark_all_read(self, *, kinds: Sequence[str] | None = None) -> int:
        """Stamp ``read_at`` on every unread row; return how many were updated.

        The bulk counterpart to :meth:`mark_read`, for an operator clearing a
        backlog. ``kinds`` optionally restricts the sweep (e.g. clear the
        ``done``/``info`` noise while leaving approvals outstanding); as in
        :meth:`unread_count`, an EMPTY sequence updates nothing rather than
        widening to everything. Already-read rows are untouched, so the stamp
        keeps the time the operator actually first read each row.
        """
        selected: list[str] | None = None
        if kinds is not None:
            selected = [str(kind) for kind in kinds]
            if not selected:
                return 0
        sql = "UPDATE notifications SET read_at = ? WHERE read_at IS NULL"
        parameters: list[Any] = [utc_now_iso()]
        if selected is not None:
            placeholders = ", ".join("?" for _ in selected)
            sql += f" AND kind IN ({placeholders})"
            parameters.extend(selected)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._connection.execute(sql, parameters)
            updated = int(cursor.rowcount or 0)
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        return updated

    @_serialized
    def mark_acted(self, notification_id: str) -> dict[str, Any] | None:
        """Stamp ``acted_at`` (and ``read_at`` if unset); return the row."""
        now = utc_now_iso()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                "UPDATE notifications SET acted_at = ?, "
                "read_at = COALESCE(read_at, ?) WHERE id = ?",
                (now, now, notification_id),
            )
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        return self.get(notification_id)


__all__ = ["NotificationPersistenceGuardRejected", "NotificationsDal"]
