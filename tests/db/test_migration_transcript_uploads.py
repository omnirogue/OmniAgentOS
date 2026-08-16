"""Binding contract for re-derived migration 099 (transcript uploads)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate

VERSION = 99
NAME = "099_transcript_uploads.sql"
NOW = "2026-07-31T00:00:00Z"


def _through(version: int) -> list[tuple[int, Path]]:
    return [(number, path) for number, path in _migration_files() if number <= version]


def _connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _employee(connection: sqlite3.Connection, employee_id: str = "emp_transcript") -> None:
    connection.execute(
        "INSERT INTO employees (id, name, status, created_at) VALUES (?,?,?,?)",
        (employee_id, employee_id, "active", NOW),
    )


def _insert(connection: sqlite3.Connection, row_id: str, employee_id: str) -> None:
    connection.execute(
        "INSERT INTO transcript_uploads "
        "(id, employee_id, filename, content_hash, size_bytes, storage_path, source, uploaded_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (row_id, employee_id, "notes.txt", "a" * 64, 3, f"{row_id}.bin", "manual", NOW),
    )


def test_099_is_packaged_under_its_own_name() -> None:
    packaged = dict(_migration_files())
    assert packaged[VERSION].name == NAME


def test_v098_upgrade_preserves_data_and_adds_exact_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = str(tmp_path / "upgrade.db")
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(98))
    assert migrate(path) == 98
    connection = _connect(path)
    _employee(connection, "emp_before_099")
    connection.close()

    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(99))
    assert migrate(path) == 99
    connection = _connect(path)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM employees WHERE id = 'emp_before_099'"
            ).fetchone()[0]
            == 1
        )
        columns = {
            row["name"]: row for row in connection.execute("PRAGMA table_info(transcript_uploads)")
        }
        assert set(columns) == {
            "id",
            "employee_id",
            "filename",
            "content_hash",
            "size_bytes",
            "storage_path",
            "source",
            "status",
            "uploaded_at",
            "analyzed_at",
            "meta_json",
        }
        expected_types = {
            "id": "TEXT",
            "employee_id": "TEXT",
            "filename": "TEXT",
            "content_hash": "TEXT",
            "size_bytes": "INTEGER",
            "storage_path": "TEXT",
            "source": "TEXT",
            "status": "TEXT",
            "uploaded_at": "TEXT",
            "analyzed_at": "TEXT",
            "meta_json": "TEXT",
        }
        assert {name: row["type"] for name, row in columns.items()} == expected_types
        assert columns["id"]["pk"] == 1
        for name in (
            "employee_id",
            "filename",
            "content_hash",
            "size_bytes",
            "storage_path",
            "source",
            "status",
            "uploaded_at",
        ):
            assert columns[name]["notnull"] == 1
        assert columns["analyzed_at"]["notnull"] == 0
        assert columns["meta_json"]["notnull"] == 0
        assert str(columns["status"]["dflt_value"]).strip("'") == "uploaded"
        foreign_keys = connection.execute("PRAGMA foreign_key_list(transcript_uploads)").fetchall()
        assert len(foreign_keys) == 1
        assert foreign_keys[0]["table"] == "employees"
        assert foreign_keys[0]["from"] == "employee_id"
        assert foreign_keys[0]["to"] == "id"
    finally:
        connection.close()


def test_unknown_employee_fk_is_refused_without_a_row(tmp_path: Path) -> None:
    path = str(tmp_path / "fk.db")
    assert migrate(path) >= VERSION
    connection = _connect(path)
    try:
        _employee(connection)
        _insert(connection, "tu_ok", "emp_transcript")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, "tu_bad", "emp_missing")
        assert connection.execute("SELECT COUNT(*) FROM transcript_uploads").fetchone()[0] == 1
    finally:
        connection.close()


def test_employee_and_status_indexes_drive_queries(tmp_path: Path) -> None:
    path = str(tmp_path / "indexes.db")
    assert migrate(path) >= VERSION
    connection = _connect(path)
    try:
        indexes = {
            row["name"] for row in connection.execute("PRAGMA index_list(transcript_uploads)")
        }
        assert {"idx_transcript_uploads_employee", "idx_transcript_uploads_status"} <= indexes
        employee_plan = " ".join(
            str(value)
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM transcript_uploads WHERE employee_id = ?",
                ("emp_transcript",),
            )
            for value in row
        )
        status_plan = " ".join(
            str(value)
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM transcript_uploads WHERE status = ?",
                ("uploaded",),
            )
            for value in row
        )
        assert "idx_transcript_uploads_employee" in employee_plan
        assert "idx_transcript_uploads_status" in status_plan
    finally:
        connection.close()


def _schema(path: str) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE tbl_name = 'transcript_uploads' ORDER BY type, name, tbl_name"
        ).fetchall()
    finally:
        connection.close()


def test_fresh_and_upgraded_transcript_schema_are_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = str(tmp_path / "fresh.db")
    upgraded = str(tmp_path / "upgraded.db")
    assert migrate(fresh) >= VERSION
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(98))
    assert migrate(upgraded) == 98
    monkeypatch.undo()
    assert migrate(upgraded) >= VERSION
    assert _schema(fresh)
    assert _schema(fresh) == _schema(upgraded)
