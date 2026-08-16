"""Append-only migration 120: routine_runs.cost_usd becomes nullable.

ISSUE-8 follow-up (Sol review, seam 2) — migration 119 made the PARENT
rollup (routines.total_cost_usd) nullable, but left the CHILD audit row
(routine_runs.cost_usd) at its original NOT NULL DEFAULT 0, so
list_runs/list_recent_runs (and the dashboard's RecentRunsPanel) kept
serving an exact $0.00 for a run whose true cost was unknown, straight from
the audit trail. NULL means "this run's cost was never reported"; DEFAULT
stays 0 because record_run's provisional placeholder for a not-yet-settled
run is a genuinely known, exact zero. Deliberately no backfill, same
rationale as 119/103/104.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate


def _through(version: int) -> list[tuple[int, Path]]:
    return [(number, path) for number, path in _migration_files() if number <= version]


def _insert_routine(connection: sqlite3.Connection, *, routine_id: str) -> None:
    connection.execute(
        """
        INSERT INTO routines (
            id, name, description, trigger_type, trigger_config_json,
            task_template_json, gate_type, gate_config_json, hard_cap_type,
            hard_cap_value, notification_target_json, status, auto_pause_reason,
            total_runs, accepted_runs, acceptance_rate, total_cost_usd,
            cost_per_accepted_change, created_at, updated_at, last_fired,
            scope, purpose, revision, neutral_runs, project_id
        ) VALUES (
            ?, ?, '', 'event', '{}', '{}', 'exit_code',
            '{}', 'max_iterations', 1, '{}', 'disabled', '',
            1, 0, NULL, 0,
            NULL, '2026-07-31T00:00:00Z', '2026-07-31T00:00:00Z', NULL,
            NULL, NULL, 0, 0, NULL
        )
        """,
        (routine_id, routine_id),
    )
    connection.commit()


def _insert_routine_run(
    connection: sqlite3.Connection,
    *,
    routine_run_id: int,
    routine_id: str,
    cost_usd: float,
) -> None:
    connection.execute(
        """
        INSERT INTO routine_runs (
            id, routine_id, run_id, iteration, gate_passed, accepted,
            cost_usd, stop_reason, notes, started_at, finished_at, created_at
        ) VALUES (?, ?, 'run_1', 1, 1, 1, ?, 'gate_passed', '', ?, ?, ?)
        """,
        (
            routine_run_id,
            routine_id,
            cost_usd,
            "2026-07-31T00:00:00Z",
            "2026-07-31T00:01:00Z",
            "2026-07-31T00:00:00Z",
        ),
    )
    connection.commit()


def test_120_drops_not_null_and_preserves_existing_zero_without_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the rebuild against silent data loss: a routine row, both
    pre-existing routine_runs indexes, and AUTOINCREMENT continuity must all
    survive alongside the cost_usd constraint change."""
    db_path = tmp_path / "upgrade-120.db"
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(119))
    assert migrate(str(db_path)) == 119
    with sqlite3.connect(db_path) as connection:
        columns = {
            str(row[1]): row
            for row in connection.execute("PRAGMA table_info(routine_runs)").fetchall()
        }
        assert columns["cost_usd"][3] == 1, "pre-120: cost_usd is still NOT NULL"
        _insert_routine(connection, routine_id="rtn_pre120")
        # explicit high id so AUTOINCREMENT continuity is checkable below.
        _insert_routine_run(connection, routine_run_id=700, routine_id="rtn_pre120", cost_usd=3.5)
        pre_indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name = 'routine_runs' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }

    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(120))
    assert migrate(str(db_path)) >= 120
    with sqlite3.connect(db_path) as connection:
        columns = {
            str(row[1]): row
            for row in connection.execute("PRAGMA table_info(routine_runs)").fetchall()
        }
        row = connection.execute("SELECT cost_usd FROM routine_runs WHERE id = 700").fetchone()
        fk_violations = connection.execute("PRAGMA foreign_key_check(routine_runs)").fetchall()

        child = connection.execute(
            "SELECT id, routine_id, cost_usd, gate_passed, accepted, stop_reason "
            "FROM routine_runs WHERE id = 700"
        ).fetchone()

        post_indexes = {
            str(r[0])
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name = 'routine_runs' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }

        connection.execute(
            "INSERT INTO routine_runs (routine_id, run_id, iteration, cost_usd, created_at) "
            "VALUES ('rtn_pre120', 'run_2', 2, 2.0, '2026-07-31T00:02:00Z')"
        )
        connection.commit()
        new_id = connection.execute(
            "SELECT id FROM routine_runs WHERE run_id = 'run_2'"
        ).fetchone()[0]

    # notnull flag is now 0 (nullable); default value is unchanged (still 0).
    assert columns["cost_usd"][3] == 0, "cost_usd must be nullable after 120"
    assert columns["cost_usd"][4] == "0", "DEFAULT must stay 0 for a not-yet-settled run"
    # No backfill: the pre-existing value is untouched.
    assert row == (3.5,)
    assert fk_violations == []

    assert child == (700, "rtn_pre120", 3.5, 1, 1, "gate_passed")
    assert pre_indexes <= post_indexes, f"pre={pre_indexes} post={post_indexes}"
    assert {"idx_routine_runs_routine_created", "idx_routine_runs_outcome"} <= post_indexes
    assert new_id > 700, "AUTOINCREMENT must continue past the pre-migration high-water mark"


def test_120_a_new_write_can_now_store_null_cost_usd(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh-120.db"
    assert migrate(str(db_path)) >= 120
    with sqlite3.connect(db_path) as connection:
        _insert_routine(connection, routine_id="rtn_unknown")
        connection.execute(
            "INSERT INTO routine_runs (routine_id, run_id, iteration, cost_usd, created_at) "
            "VALUES ('rtn_unknown', 'run_unknown', 1, NULL, '2026-07-31T00:00:00Z')"
        )
        connection.commit()
        row = connection.execute(
            "SELECT cost_usd FROM routine_runs WHERE run_id = 'run_unknown'"
        ).fetchone()
    assert row == (None,)


def test_fresh_database_has_latest_120_revision_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh-120-versions.db"
    assert migrate(str(db_path)) >= 120
    with sqlite3.connect(db_path) as connection:
        versions = connection.execute(
            "SELECT MAX(version), COUNT(*) FROM schema_migrations WHERE version = 120"
        ).fetchone()
        cost_usd = next(
            row
            for row in connection.execute("PRAGMA table_info(routine_runs)").fetchall()
            if row[1] == "cost_usd"
        )
    assert versions == (120, 1)
    assert cost_usd[2].upper() == "REAL"
    assert cost_usd[3] == 0
    assert cost_usd[4] == "0"
