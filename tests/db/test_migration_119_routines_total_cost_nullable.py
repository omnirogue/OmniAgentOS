"""Append-only migration 119: routines.total_cost_usd becomes nullable.

ISSUE-8 — NULL means "unknown" (at least one contributing run's cost was
never reported); the DEFAULT stays 0 because a routine that has fired zero
times has a genuinely exact zero cost. Deliberately no backfill: an existing
0 predating this migration is indistinguishable from a quietly undercounted
total, so it is left exactly as it was (same rationale migrations 103/104
used for their own no-backfill decisions).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate


def _through(version: int) -> list[tuple[int, Path]]:
    return [(number, path) for number, path in _migration_files() if number <= version]


def _insert_pre_119_routine(
    connection: sqlite3.Connection, *, routine_id: str, total_cost_usd: float
) -> None:
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
            4, 0, NULL, ?,
            NULL, '2026-07-31T00:00:00Z', '2026-07-31T00:00:00Z', NULL,
            NULL, NULL, 0, 0, NULL
        )
        """,
        (routine_id, routine_id, total_cost_usd),
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


def test_119_drops_not_null_and_preserves_existing_zero_without_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sol review (concern 3): pins the migration's rebuild against silent
    data loss — a CHILD row (routine_runs), both pre-existing indexes, and
    AUTOINCREMENT continuity must all survive the routines + routine_runs
    rebuild, not just the total_cost_usd column this migration exists for."""
    db_path = tmp_path / "upgrade-119.db"
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(118))
    assert migrate(str(db_path)) == 118
    with sqlite3.connect(db_path) as connection:
        columns = {
            str(row[1]): row for row in connection.execute("PRAGMA table_info(routines)").fetchall()
        }
        assert columns["total_cost_usd"][3] == 1, "pre-119: total_cost_usd is still NOT NULL"
        _insert_pre_119_routine(connection, routine_id="rtn_pre119", total_cost_usd=0.0)
        # A CHILD row, explicitly id=500 so post-migration AUTOINCREMENT
        # continuity is checkable below (a naive rebuild that lost the
        # sqlite_sequence tracking would let a fresh insert collide with, or
        # fall behind, this id).
        _insert_routine_run(connection, routine_run_id=500, routine_id="rtn_pre119", cost_usd=1.25)
        pre_indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name IN ('routines', 'routine_runs') AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }

    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(119))
    assert migrate(str(db_path)) >= 119
    with sqlite3.connect(db_path) as connection:
        columns = {
            str(row[1]): row for row in connection.execute("PRAGMA table_info(routines)").fetchall()
        }
        row = connection.execute(
            "SELECT total_cost_usd FROM routines WHERE id = 'rtn_pre119'"
        ).fetchone()
        fk_violations = connection.execute("PRAGMA foreign_key_check(routines)").fetchall()
        fk_violations += connection.execute("PRAGMA foreign_key_check(routine_runs)").fetchall()

        # --- child row survives the rebuild, byte-for-byte ------------------
        child = connection.execute(
            "SELECT id, routine_id, cost_usd, gate_passed, accepted, stop_reason "
            "FROM routine_runs WHERE id = 500"
        ).fetchone()

        # --- both pre-existing indexes survive -------------------------------
        post_indexes = {
            str(r[0])
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name IN ('routines', 'routine_runs') AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }

        # --- AUTOINCREMENT continues past the highest pre-migration id ------
        connection.execute(
            "INSERT INTO routine_runs (routine_id, run_id, iteration, cost_usd, created_at) "
            "VALUES ('rtn_pre119', 'run_2', 2, 2.0, '2026-07-31T00:02:00Z')"
        )
        connection.commit()
        new_id = connection.execute(
            "SELECT id FROM routine_runs WHERE run_id = 'run_2'"
        ).fetchone()[0]

    # notnull flag is now 0 (nullable); default value is unchanged (still 0).
    assert columns["total_cost_usd"][3] == 0, "total_cost_usd must be nullable after 119"
    assert columns["total_cost_usd"][4] == "0", "DEFAULT must stay 0 for brand-new routines"
    # No backfill: the pre-existing 0 is untouched, not rewritten to NULL.
    assert row == (0.0,)
    assert fk_violations == []

    assert child == (500, "rtn_pre119", 1.25, 1, 1, "gate_passed")

    # Every index present through 118 (idx_routines_status, idx_routines_project
    # — unconditionally created by migration 116, nothing version-gated about
    # it — idx_routine_runs_routine_created, idx_routine_runs_outcome) must
    # survive the rebuild.
    assert pre_indexes <= post_indexes, f"pre={pre_indexes} post={post_indexes}"

    assert new_id > 500, "AUTOINCREMENT must continue past the pre-migration high-water mark"


def test_119_a_new_write_can_now_store_null_total_cost(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh-119.db"
    assert migrate(str(db_path)) >= 119
    with sqlite3.connect(db_path) as connection:
        _insert_pre_119_routine(connection, routine_id="rtn_unknown", total_cost_usd=None)  # type: ignore[arg-type]
        row = connection.execute(
            "SELECT total_cost_usd FROM routines WHERE id = 'rtn_unknown'"
        ).fetchone()
    assert row == (None,)


def test_fresh_database_has_latest_119_revision_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh-119-versions.db"
    assert migrate(str(db_path)) >= 119
    with sqlite3.connect(db_path) as connection:
        versions = connection.execute(
            "SELECT MAX(version), COUNT(*) FROM schema_migrations WHERE version = 119"
        ).fetchone()
        total_cost_usd = next(
            row
            for row in connection.execute("PRAGMA table_info(routines)").fetchall()
            if row[1] == "total_cost_usd"
        )
    assert versions == (119, 1)
    assert total_cost_usd[2].upper() == "REAL"
    assert total_cost_usd[3] == 0
    assert total_cost_usd[4] == "0"
