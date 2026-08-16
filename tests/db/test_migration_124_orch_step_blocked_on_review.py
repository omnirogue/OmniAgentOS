"""Append-only migration 124: orchestration_steps accepts 'blocked_on_review'.

Redteam addendum (c) — 044's inline CHECK enumerated the checkpoint status
vocabulary without 'blocked_on_review', the status ``orchestrator/core.py``
writes for an H2 reviewer-infrastructure failure. ``step_finished`` swallows
``sqlite3.Error``, so that write was rejected and lost in silence.

These tests pin the rebuild against the two ways it can go wrong: losing rows
or indexes, and "widening" the vocabulary by deleting the constraint.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate

# Every status 044 allowed, in seq order — all of them must survive the rebuild.
LEGACY_STATUSES = ("pending", "running", "done", "unreviewed", "denied", "failed")


def _through(version: int) -> list[tuple[int, Path]]:
    return [(number, path) for number, path in _migration_files() if number <= version]


def _steps(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    return connection.execute(
        "SELECT run_id, seq, title, status, session_id, attempts, output_tail, updated_at "
        "FROM orchestration_steps ORDER BY run_id, seq"
    ).fetchall()


def _indexes(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name = 'orchestration_steps' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _insert_step(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    seq: int,
    status: str,
    session_id: str | None,
) -> None:
    connection.execute(
        "INSERT INTO orchestration_steps "
        "(run_id, seq, title, status, session_id, attempts, output_tail, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            seq,
            f"step {seq}",
            status,
            session_id,
            seq,
            f"tail {seq}",
            "2026-08-10T00:00:00Z",
        ),
    )


def test_124_widens_the_status_check_and_preserves_every_row_and_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "upgrade-124.db"
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(123))
    assert migrate(str(db_path)) == 123

    with sqlite3.connect(db_path) as connection:
        for seq, status in enumerate(LEGACY_STATUSES):
            _insert_step(
                connection,
                run_id="orch_pre124",
                seq=seq,
                status=status,
                # A NULL session_id exercises the PARTIAL index's predicate.
                session_id=None if status == "pending" else f"ses_{seq}",
            )
        connection.commit()
        # Pre-124 the H2 status is refused outright — this is the live defect.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_step(
                connection,
                run_id="orch_pre124",
                seq=99,
                status="blocked_on_review",
                session_id="ses_99",
            )
        pre_rows = _steps(connection)
        pre_indexes = _indexes(connection)

    assert pre_indexes == {"idx_orch_steps_session"}

    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(124))
    assert migrate(str(db_path)) >= 124

    with sqlite3.connect(db_path) as connection:
        post_rows = _steps(connection)
        post_indexes = _indexes(connection)
        fk_violations = connection.execute(
            "PRAGMA foreign_key_check(orchestration_steps)"
        ).fetchall()
        primary_key = [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(orchestration_steps)").fetchall()
            if row[5]
        ]
        # The whole point: the previously-rejected write now lands.
        _insert_step(
            connection,
            run_id="orch_pre124",
            seq=99,
            status="blocked_on_review",
            session_id="ses_99",
        )
        connection.commit()
        landed = connection.execute(
            "SELECT status FROM orchestration_steps WHERE run_id = ? AND seq = ?",
            ("orch_pre124", 99),
        ).fetchone()
        # …and the constraint is still a constraint (mutation catcher: a rebuild
        # that simply DROPPED the CHECK would satisfy every assertion above).
        with pytest.raises(sqlite3.IntegrityError):
            _insert_step(
                connection,
                run_id="orch_pre124",
                seq=100,
                status="blocked_on_reviewww",
                session_id=None,
            )

    assert post_rows == pre_rows, "the rebuild must copy every column of every row"
    assert post_indexes == pre_indexes
    assert fk_violations == []
    assert primary_key == ["run_id", "seq"]
    assert landed == ("blocked_on_review",)


def test_fresh_database_has_the_widened_orchestration_step_vocabulary(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh-124.db"
    assert migrate(str(db_path)) >= 124
    with sqlite3.connect(db_path) as connection:
        versions = connection.execute(
            "SELECT MAX(version), COUNT(*) FROM schema_migrations WHERE version = 124"
        ).fetchone()
        status = next(
            row
            for row in connection.execute("PRAGMA table_info(orchestration_steps)").fetchall()
            if row[1] == "status"
        )
        for seq, value in enumerate((*LEGACY_STATUSES, "blocked_on_review")):
            _insert_step(connection, run_id="orch_fresh", seq=seq, status=value, session_id=None)
        connection.commit()
        stored = [
            str(row[0])
            for row in connection.execute(
                "SELECT status FROM orchestration_steps WHERE run_id = 'orch_fresh' ORDER BY seq"
            ).fetchall()
        ]
    assert versions == (124, 1)
    assert status[3] == 1, "status must stay NOT NULL"
    assert status[4] == "'pending'", "the DEFAULT must stay 'pending'"
    assert stored == [*LEGACY_STATUSES, "blocked_on_review"]
