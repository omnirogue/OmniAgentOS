"""Migration 091: expand routines.gate_type CHECK for merge_candidate.

Critical production path: a DB already populated with routines + routine_runs
must upgrade under migrate()'s BEGIN IMMEDIATE. PRAGMA foreign_keys=OFF is a
no-op inside that transaction, so the migration must rebuild the child first
(same pattern as 089) rather than dropping the parent while children exist.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _migrate_through(version: int, db_path: str, monkeypatch: pytest.MonkeyPatch) -> int:
    packaged = _migration_files()
    subset = [(v, p) for v, p in packaged if v <= version]
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: subset)
    return migrate(db_path)


def test_091_upgrades_populated_routines_with_child_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance: 091 upgrades a v090 DB that already has parent+child rows.

    Failing-on-revert: restoring a migration that DROP TABLE routines while
    routine_runs still references it (or relies on in-transaction
    PRAGMA foreign_keys=OFF) raises IntegrityError under BEGIN IMMEDIATE.
    """
    packaged = _migration_files()
    assert any(v == 91 for v, _ in packaged), "091_routines_merge_candidate_gate.sql must be present"
    assert max(v for v, _ in packaged) >= 91

    db_path = str(tmp_path / "populated_v090.db")
    assert _migrate_through(90, db_path, monkeypatch) == 90

    connection = _connect(db_path)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        now = "2026-07-29T12:00:00Z"
        connection.execute(
            """
            INSERT INTO routines (
                id, name, description, trigger_type, trigger_config_json,
                task_template_json, gate_type, gate_config_json, hard_cap_type,
                hard_cap_value, notification_target_json, status, auto_pause_reason,
                total_runs, accepted_runs, acceptance_rate, total_cost_usd,
                cost_per_accepted_change, created_at, updated_at, last_fired
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                "rtn_pre_091",
                "pre-091-routine",
                "existing production row",
                "cron",
                '{"cron":"0 * * * *"}',
                '{"title":"pre"}',
                "exit_code",
                '{"expected":0}',
                "max_iterations",
                3.0,
                "{}",
                "active",
                "",
                1,
                1,
                1.0,
                0.5,
                0.5,
                now,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO routine_runs (
                routine_id, run_id, iteration, gate_passed, accepted, cost_usd,
                stop_reason, notes, started_at, finished_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rtn_pre_091",
                "run_pre_091",
                1,
                1,
                1,
                0.5,
                "gate_passed",
                "pre-migration child",
                now,
                now,
                now,
            ),
        )
        child_count = connection.execute(
            "SELECT COUNT(*) AS n FROM routine_runs WHERE routine_id = ?",
            ("rtn_pre_091",),
        ).fetchone()["n"]
        assert child_count == 1
    finally:
        connection.close()

    # Advance through 091 via the real migrate path (BEGIN IMMEDIATE + apply).
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: packaged)
    assert migrate(db_path) >= 91

    connection = _connect(db_path)
    try:
        applied = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 91"
        ).fetchone()
        assert applied is not None

        parent = connection.execute(
            "SELECT id, name, gate_type, total_runs FROM routines WHERE id = ?",
            ("rtn_pre_091",),
        ).fetchone()
        assert parent is not None
        assert parent["name"] == "pre-091-routine"
        assert parent["gate_type"] == "exit_code"
        assert parent["total_runs"] == 1

        child = connection.execute(
            "SELECT routine_id, run_id, notes FROM routine_runs WHERE routine_id = ?",
            ("rtn_pre_091",),
        ).fetchone()
        assert child is not None
        assert child["run_id"] == "run_pre_091"
        assert child["notes"] == "pre-migration child"

        # Expanded CHECK accepts merge_candidate after the rebuild.
        connection.execute(
            """
            INSERT INTO routines (
                id, name, description, trigger_type, trigger_config_json,
                task_template_json, gate_type, gate_config_json, hard_cap_type,
                hard_cap_value, notification_target_json, status, auto_pause_reason,
                total_runs, accepted_runs, acceptance_rate, total_cost_usd,
                cost_per_accepted_change, created_at, updated_at, last_fired
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                "rtn_merge_candidate",
                "merge-candidate-routine",
                "",
                "event",
                '{"event":"merge"}',
                "{}",
                "merge_candidate",
                '{"candidate_sha":"a"*40,"merge_base_sha":"b"*40}',
                "max_iterations",
                1.0,
                "{}",
                "active",
                "",
                0,
                0,
                None,
                0.0,
                None,
                now,
                now,
                None,
            ),
        )
        stored = connection.execute(
            "SELECT gate_type FROM routines WHERE id = ?",
            ("rtn_merge_candidate",),
        ).fetchone()
        assert stored is not None
        assert stored["gate_type"] == "merge_candidate"

        # FK still enforces parent existence after rebuild.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO routine_runs (
                    routine_id, run_id, iteration, gate_passed, accepted, cost_usd,
                    stop_reason, notes, started_at, finished_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "missing_parent",
                    "orphan",
                    1,
                    0,
                    0,
                    0.0,
                    "",
                    "",
                    now,
                    now,
                    now,
                ),
            )
    finally:
        connection.close()
