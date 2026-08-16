"""Append-only migration 097: monotonic routine revision token."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate


def _through(version: int) -> list[tuple[int, Path]]:
    return [(number, path) for number, path in _migration_files() if number <= version]


def _insert_pre_097_routine(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO routines (
            id, name, description, trigger_type, trigger_config_json,
            task_template_json, gate_type, gate_config_json, hard_cap_type,
            hard_cap_value, notification_target_json, status, auto_pause_reason,
            total_runs, accepted_runs, acceptance_rate, total_cost_usd,
            cost_per_accepted_change, created_at, updated_at, last_fired,
            scope, purpose
        ) VALUES (
            'rtn_pre097', 'pre-097', '', 'event', '{}', '{}', 'exit_code',
            '{}', 'max_iterations', 1, '{}', 'disabled', '', 0, 0, NULL,
            0, NULL, '2026-07-31T00:00:00Z', '2026-07-31T00:00:00Z',
            NULL, NULL, NULL
        )
        """
    )
    connection.commit()


def test_097_adds_non_null_revision_and_preserves_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "upgrade-097.db"
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(96))
    assert migrate(str(db_path)) == 96
    with sqlite3.connect(db_path) as connection:
        _insert_pre_097_routine(connection)
        assert "revision" not in {
            str(row[1]) for row in connection.execute("PRAGMA table_info(routines)")
        }

    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(97))
    assert migrate(str(db_path)) >= 97
    with sqlite3.connect(db_path) as connection:
        columns = {
            str(row[1]): row for row in connection.execute("PRAGMA table_info(routines)").fetchall()
        }
        row = connection.execute(
            "SELECT name, revision FROM routines WHERE id = 'rtn_pre097'"
        ).fetchone()

    assert columns["revision"][2].upper() == "INTEGER"
    assert columns["revision"][3] == 1
    assert columns["revision"][4] == "0"
    assert row == ("pre-097", 0)


def test_fresh_database_has_latest_097_revision_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh-097.db"
    assert migrate(str(db_path)) >= 97
    with sqlite3.connect(db_path) as connection:
        versions = connection.execute(
            "SELECT MAX(version), COUNT(*) FROM schema_migrations WHERE version = 97"
        ).fetchone()
        revision = next(
            row
            for row in connection.execute("PRAGMA table_info(routines)").fetchall()
            if row[1] == "revision"
        )

    assert versions == (97, 1)
    assert revision[2].upper() == "INTEGER"
    assert revision[3] == 1
    assert revision[4] == "0"
