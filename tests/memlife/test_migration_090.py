"""Migration 090 (memlife): applies cleanly on a schema frozen at v089."""

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


def test_090_applies_cleanly_on_copy_of_live_schema_at_v089(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance: migration 090 applies on a DB that stopped at v089."""
    packaged = _migration_files()
    assert any(v == 90 for v, _ in packaged), "090_memlife.sql must be present"
    assert max(v for v, _ in packaged) >= 90

    up_to_089 = [(v, p) for v, p in packaged if v <= 89]
    assert max(v for v, _ in up_to_089) == 89

    db_path = str(tmp_path / "at_v089.db")
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: up_to_089)
    version = migrate(db_path)
    assert version == 89

    # Restore full set (including 090) and advance.
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: packaged)
    version = migrate(db_path)
    assert version >= 90

    connection = _connect(db_path)
    try:
        applied = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 90"
        ).fetchall()
        assert len(applied) == 1

        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "memlife_candidates",
            "memlife_decisions",
            "memlife_lessons",
        } <= tables

        # Status CHECK matches CandidateStatus / DecisionAction / LessonStatus.
        cols = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(memlife_candidates)").fetchall()
        }
        assert {
            "id",
            "key",
            "claim",
            "conditions",
            "evidence_ids_json",
            "cluster_size",
            "status",
            "rejection_count",
            "salience",
            "created_at",
            "updated_at",
        } <= cols
    finally:
        connection.close()


def test_090_decisions_are_append_only(tmp_path: Path) -> None:
    """Append-only decision log: UPDATE/DELETE must be refused."""
    db_path = str(tmp_path / "append.db")
    assert migrate(db_path) >= 90
    connection = _connect(db_path)
    try:
        now = "2026-07-28T12:00:00Z"
        connection.execute(
            """
            INSERT INTO memlife_candidates (
                id, key, claim, conditions, evidence_ids_json, cluster_size,
                status, rejection_count, salience, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cand_1",
                "k",
                "claim text",
                "",
                "[]",
                1,
                "staged",
                0,
                None,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO memlife_decisions (id, candidate_id, action, at, actor, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("dec_1", "cand_1", "stage", now, "dream-cycle", ""),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only|immutable"):
            connection.execute(
                "UPDATE memlife_decisions SET reason = 'tamper' WHERE id = ?",
                ("dec_1",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only|immutable"):
            connection.execute("DELETE FROM memlife_decisions WHERE id = ?", ("dec_1",))
    finally:
        connection.close()
