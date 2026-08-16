"""Migration 044 (swarm schema): tables, columns, CHECKs, indexes, backfill."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    db = str(tmp_path / "swarm.db")
    migrate(db)
    return db


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def test_044_tables_columns_and_indexes_exist(db_path: str) -> None:
    connection = _connect(db_path)
    try:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"swarm_runs", "swarm_deps", "swarm_attempts"} <= tables

        assert {"swarm_run_id", "swarm_json"} <= _columns(connection, "board_tasks")
        assert "account_id" in _columns(connection, "sessions")
        assert {"output_text", "idle_minutes"} <= _columns(connection, "sessions")
        assert "provider" in _columns(connection, "claude_accounts")
        # The frozen 043 surfaces are untouched.
        assert {"category_id", "lane", "park_state"} <= _columns(connection, "board_tasks")

        indexes = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        assert {
            "idx_swarm_runs_status",
            "idx_swarm_deps_run",
            "idx_swarm_attempts_run",
            "idx_swarm_attempts_live",
            "idx_sessions_account_state",
            "idx_board_tasks_swarm",
        } <= indexes
    finally:
        connection.close()


def test_044_status_and_end_reason_checks_enforced(db_path: str) -> None:
    connection = _connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO swarm_runs (id, status, created_at, updated_at) "
                "VALUES ('swr_bad', 'sprinting', '2026-07-23T00:00:00Z', '2026-07-23T00:00:00Z')"
            )
        connection.execute(
            "INSERT INTO swarm_attempts "
            "(id, swarm_run_id, board_task_id, seq, provider, model, started_at) "
            "VALUES ('swa_1', 'swr_1', 'btk_1', 0, 'claude', 'sonnet', '2026-07-23T00:00:00Z')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE swarm_attempts SET end_reason = 'wandered_off' WHERE id = 'swa_1'"
            )
    finally:
        connection.close()


def test_044_one_live_attempt_partial_unique_index(db_path: str) -> None:
    """idx_swarm_attempts_live (043's idiom) enforces one live attempt at SQL level."""
    connection = _connect(db_path)
    try:
        connection.execute(
            "INSERT INTO swarm_attempts "
            "(id, swarm_run_id, board_task_id, seq, provider, model, started_at) "
            "VALUES ('swa_a', 'swr_1', 'btk_1', 0, 'claude', 'sonnet', '2026-07-23T00:00:00Z')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO swarm_attempts "
                "(id, swarm_run_id, board_task_id, seq, provider, model, started_at) "
                "VALUES ('swa_b', 'swr_1', 'btk_1', 1, 'codex', 'sol', '2026-07-23T00:00:01Z')"
            )
        # Closing the live attempt frees the slot; UNIQUE(board_task_id, seq) still holds.
        connection.execute(
            "UPDATE swarm_attempts SET ended_at = '2026-07-23T00:01:00Z', "
            "end_reason = 'completed' WHERE id = 'swa_a'"
        )
        connection.execute(
            "INSERT INTO swarm_attempts "
            "(id, swarm_run_id, board_task_id, seq, provider, model, started_at) "
            "VALUES ('swa_b', 'swr_1', 'btk_1', 1, 'codex', 'sol', '2026-07-23T00:02:00Z')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO swarm_attempts "
                "(id, swarm_run_id, board_task_id, seq, provider, model, started_at, "
                "ended_at, end_reason) "
                "VALUES ('swa_c', 'swr_1', 'btk_1', 1, 'grok', 'g4', '2026-07-23T00:03:00Z', "
                "'2026-07-23T00:04:00Z', 'crashed')"
            )
    finally:
        connection.close()


def test_044_backfills_provider_on_pre_existing_accounts(tmp_path: Path) -> None:
    """A claude_accounts row created BEFORE 044 reads provider='claude' after it."""
    db = str(tmp_path / "backfill.db")
    connection = sqlite3.connect(db, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        for version, path in _migration_files():
            if version >= 44:
                continue
            script = path.read_text(encoding="utf-8")
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                f"{script}\n"
                "INSERT INTO schema_migrations (version, applied_at) "
                f"VALUES ({version}, '2026-07-23T00:00:00Z');\n"
                "COMMIT;\n"
            )
        connection.execute(
            "INSERT INTO claude_accounts (id, label, created_at, updated_at) "
            "VALUES ('acc_old', 'pre-044', '2026-07-23T00:00:00Z', '2026-07-23T00:00:00Z')"
        )
    finally:
        connection.close()

    migrate(db)

    check = sqlite3.connect(db, isolation_level=None)
    check.row_factory = sqlite3.Row
    try:
        row = check.execute("SELECT provider FROM claude_accounts WHERE id = 'acc_old'").fetchone()
        assert row["provider"] == "claude"
    finally:
        check.close()
