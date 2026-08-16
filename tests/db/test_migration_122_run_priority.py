"""Append-only migration 122: durable run priority and claim-order index."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate
from omniagentos.db.store import SqliteStore


def _through(version: int) -> list[tuple[int, Path]]:
    return [(number, path) for number, path in _migration_files() if number <= version]


def test_122_backfills_existing_runs_to_normal_and_keeps_them_claimable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "upgrade-122.db"
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(121))
    assert migrate(str(db_path)) == 121
    with sqlite3.connect(db_path) as connection:
        now = "2026-08-09T12:00:00Z"
        connection.execute(
            "INSERT INTO tasks (id, title, state, created_at, updated_at) "
            "VALUES ('tsk_pre122', 'legacy', 'ready', ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO runs (id, task_id, harness, trace_id, queued_at, created_at, updated_at) "
            "VALUES ('run_pre122', 'tsk_pre122', 'mock', 'trace-pre122', ?, ?, ?)",
            (now, now, now),
        )
        connection.commit()

    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(122))
    assert migrate(str(db_path)) == 122
    store = SqliteStore(str(db_path))
    claimed = store.claim_next_run("legacy-worker")
    assert claimed is not None
    assert claimed["id"] == "run_pre122"
    assert claimed["priority"] == 2


def test_122_fresh_schema_has_default_and_claim_index(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh-122.db"
    assert migrate(str(db_path)) >= 122
    with sqlite3.connect(db_path) as connection:
        priority = next(
            row for row in connection.execute("PRAGMA table_info(runs)") if row[1] == "priority"
        )
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(runs)").fetchall()
        }
        versions = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 122"
        ).fetchone()
    assert priority[2].upper() == "INTEGER"
    assert priority[3] == 1
    assert priority[4] == "2"
    assert "idx_runs_state_priority_queued" in indexes
    assert versions == (1,)


def test_122_rejects_priority_outside_shared_taxonomy(tmp_path: Path) -> None:
    db_path = tmp_path / "priority-check-122.db"
    assert migrate(str(db_path)) >= 122
    with sqlite3.connect(db_path) as connection:
        now = "2026-08-09T12:00:00Z"
        connection.execute(
            "INSERT INTO tasks (id, title, state, created_at, updated_at) "
            "VALUES ('tsk_priority_check', 'check', 'ready', ?, ?)",
            (now, now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO runs "
                "(id, task_id, harness, priority, trace_id, queued_at, created_at, updated_at) "
                "VALUES ('run_priority_check', 'tsk_priority_check', 'mock', 4, "
                "'trace-check', ?, ?, ?)",
                (now, now, now),
            )
