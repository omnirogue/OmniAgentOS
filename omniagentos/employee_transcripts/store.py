"""SQLite DAL for migration 099's ``transcript_uploads`` table."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from functools import wraps
from typing import Any, cast

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore, _row, _rows
from omniagentos.employee_transcripts.models import (
    TRANSCRIPT_ID_PREFIX,
    TRANSCRIPT_SOURCES,
    TRANSCRIPT_STATUSES,
)

TRANSCRIPT_UPLOADS_TABLE = "transcript_uploads"
EMPLOYEES_TABLE = "employees"  # read-only FK far side, owned by company_goals
_UPDATE_FIELDS = frozenset({"status", "analyzed_at", "meta_json"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DeleteFile = Callable[[dict[str, Any]], None]


def _serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        dal = cast("TranscriptStore", args[0])
        with dal._store._lock:
            return method(*args, **kwargs)

    return wrapped


def _validate_source(source: str) -> None:
    if source not in TRANSCRIPT_SOURCES:
        raise ValueError(f"source must be one of {sorted(TRANSCRIPT_SOURCES)}")


def _validate_status(status: str) -> None:
    if status not in TRANSCRIPT_STATUSES:
        raise ValueError(f"status must be one of {sorted(TRANSCRIPT_STATUSES)}")


class TranscriptStore:
    """Transcript DAL composed over the base store's live thread connection."""

    def __init__(self, store: SqliteStore) -> None:
        self._store = store

    @property
    def _connection(self) -> sqlite3.Connection:
        """Resolve the calling thread's connection live; never cache it."""
        return self._store._connection

    @_serialized
    def employee_exists(self, employee_id: str) -> bool:
        return (
            self._connection.execute(
                f"SELECT 1 FROM {EMPLOYEES_TABLE} WHERE id = ?", (employee_id,)
            ).fetchone()
            is not None
        )

    @_serialized
    def create(
        self,
        *,
        employee_id: str,
        filename: str,
        content_hash: str,
        size_bytes: int,
        storage_path: str,
        source: str = "manual",
        status: str = "uploaded",
        uploaded_at: str | None = None,
        analyzed_at: str | None = None,
        meta_json: str | None = None,
        transcript_id: str | None = None,
    ) -> dict[str, Any]:
        _validate_source(source)
        _validate_status(status)
        if not filename:
            raise ValueError("filename is required")
        if not _SHA256_RE.fullmatch(content_hash):
            raise ValueError("content_hash must be lowercase SHA-256 hex")
        if size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")
        if not storage_path:
            raise ValueError("storage_path is required")
        row_id = transcript_id or new_id(TRANSCRIPT_ID_PREFIX)
        parameters = (
            row_id,
            employee_id,
            filename,
            content_hash,
            int(size_bytes),
            storage_path,
            source,
            status,
            uploaded_at or utc_now_iso(),
            analyzed_at,
            meta_json,
        )

        def body(connection: sqlite3.Connection) -> dict[str, Any]:
            connection.execute(
                f"INSERT INTO {TRANSCRIPT_UPLOADS_TABLE} "
                "(id, employee_id, filename, content_hash, size_bytes, storage_path, "
                "source, status, uploaded_at, analyzed_at, meta_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                parameters,
            )
            created = _row(
                connection.execute(
                    f"SELECT * FROM {TRANSCRIPT_UPLOADS_TABLE} WHERE id = ?", (row_id,)
                ).fetchone()
            )
            assert created is not None
            return created

        return self._store._execute_write_txn(body, op="employee_transcripts.create")

    @_serialized
    def get(self, transcript_id: str) -> dict[str, Any] | None:
        return _row(
            self._connection.execute(
                f"SELECT * FROM {TRANSCRIPT_UPLOADS_TABLE} WHERE id = ?", (transcript_id,)
            ).fetchone()
        )

    @_serialized
    def list(
        self,
        *,
        employee_id: str | None = None,
        status: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 5000:
            raise ValueError("limit must be between 1 and 5000")
        if status is not None:
            _validate_status(status)
        clauses: list[str] = []
        parameters: list[Any] = []
        if employee_id is not None:
            clauses.append("employee_id = ?")
            parameters.append(employee_id)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        sql = f"SELECT * FROM {TRANSCRIPT_UPLOADS_TABLE}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY uploaded_at DESC, id DESC LIMIT ?"
        parameters.append(int(limit))
        return _rows(self._connection.execute(sql, parameters).fetchall())

    @_serialized
    def update(self, transcript_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
        unknown = values.keys() - _UPDATE_FIELDS
        if unknown:
            raise ValueError(f"unknown transcript columns: {', '.join(sorted(unknown))}")
        if "status" in values:
            _validate_status(str(values["status"]))
        if not values:
            return self.get(transcript_id)
        columns = sorted(values)
        assignments = ", ".join(f"{column} = ?" for column in columns)
        changed = self._store._write_count(
            f"UPDATE {TRANSCRIPT_UPLOADS_TABLE} SET {assignments} WHERE id = ?",
            [values[column] for column in columns] + [transcript_id],
        )
        return self.get(transcript_id) if changed else None

    @_serialized
    def delete(self, transcript_id: str) -> bool:
        return (
            self._store._write_count(
                f"DELETE FROM {TRANSCRIPT_UPLOADS_TABLE} WHERE id = ?", (transcript_id,)
            )
            > 0
        )

    @_serialized
    def delete_with(self, transcript_id: str, before_delete: DeleteFile) -> bool:
        """Run a file transition and row deletion in one serialized DB transaction."""

        def body(connection: sqlite3.Connection) -> bool:
            current = _row(
                connection.execute(
                    f"SELECT * FROM {TRANSCRIPT_UPLOADS_TABLE} WHERE id = ?", (transcript_id,)
                ).fetchone()
            )
            if current is None:
                return False
            before_delete(current)
            connection.execute(
                f"DELETE FROM {TRANSCRIPT_UPLOADS_TABLE} WHERE id = ?", (transcript_id,)
            )
            return True

        return self._store._execute_write_txn(body, op="employee_transcripts.delete")
