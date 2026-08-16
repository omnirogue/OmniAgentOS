"""Exact historical repair and real migration matrix for migration 096."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_096 = ROOT / "omniagentos/db/migrations/096_dag_moe_gating.sql"
BROKEN_096 = ROOT / "tests/db/fixtures/096_dag_moe_gating_broken.sql"
REPAIR_RECORD = ROOT / "omniagentos/db/migration_repairs/096_dag_moe_gating.json"
AUTHORITY_GATE = ROOT / "scripts/git-hooks/check-migration-versions.sh"
OLD_SHA256 = "c01d0ff3fae6fc0cb000fdfd2af21b44c80f6ce8a71ce85a8fc06d6e517cc554"
NEW_SHA256 = "6752fac84728d5ef31030f0755a39bc8cdb2bb5c87f5fb79ec2ae8df3ae94e8a"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _through(version: int) -> list[tuple[int, Path]]:
    return [(number, path) for number, path in _migration_files() if number <= version]


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _assert_096_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        graph_columns = _columns(connection, "graph_edges")
        dag_columns = _columns(connection, "dag_step_edges")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        checksum = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 96"
        ).fetchone()

    assert {"graph_run_id", "from_node_key", "to_node_key"} <= graph_columns
    assert "parent_step_id" not in graph_columns
    assert {"parent_step_id", "child_step_id"} <= dag_columns
    assert "graph_run_id" not in dag_columns
    assert "moe_gates" in tables
    assert checksum == (NEW_SHA256,)


def test_repair_record_and_authority_are_bound_to_exact_hashes() -> None:
    record = json.loads(REPAIR_RECORD.read_text(encoding="utf-8"))
    authority = AUTHORITY_GATE.read_text(encoding="utf-8")

    assert record["migration_version"] == 96
    assert record["migration_path"].endswith("/096_dag_moe_gating.sql")
    assert record["old_sha256"] == OLD_SHA256 == _sha256(BROKEN_096)
    assert record["new_sha256"] == NEW_SHA256 == _sha256(MIGRATION_096)
    assert record["failure_evidence"]["kind"] == "sqlite_duplicate_table"
    assert record["live_checksum_evidence"]["recorded_sha256"] == NEW_SHA256
    assert "not a general append-only bypass" in record["policy"]
    assert OLD_SHA256 in authority
    assert NEW_SHA256 in authority


def test_original_096_fails_after_real_062_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "original-096.db"
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(95))
    assert migrate(str(db_path)) == 95

    broken_files = [
        (version, BROKEN_096 if version == 96 else path) for version, path in _through(96)
    ]
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: broken_files)
    with pytest.raises(sqlite3.OperationalError, match="graph_edges already exists"):
        migrate(str(db_path))

    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute("SELECT 1 FROM schema_migrations WHERE version = 96").fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='moe_gates'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("starting_version", [None, 61, 62, 94, 95])
def test_real_fresh_and_upgrade_matrix_to_096(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    starting_version: int | None,
) -> None:
    db_path = tmp_path / f"from-{starting_version or 'empty'}.db"
    if starting_version is not None:
        monkeypatch.setattr(
            "omniagentos.db.migrate._migration_files",
            lambda: _through(starting_version),
        )
        assert migrate(str(db_path)) == starting_version

    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(96))
    assert migrate(str(db_path)) == 96
    _assert_096_schema(db_path)

    with sqlite3.connect(db_path) as connection:
        first = connection.execute(
            "SELECT version, applied_at, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert migrate(str(db_path)) == 96
    with sqlite3.connect(db_path) as connection:
        second = connection.execute(
            "SELECT version, applied_at, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert second == first


@pytest.mark.parametrize("copy_name", ["runtime-state.sqlite3", "var-omniagentos.db"])
def test_copied_authoritative_096_database_verifies_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    copy_name: str,
) -> None:
    """Hermetic equivalent of copied-live checksum/no-op verification."""
    authority = tmp_path / "authority.db"
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(96))
    assert migrate(str(authority)) == 96
    copied = tmp_path / copy_name
    shutil.copy2(authority, copied)

    with sqlite3.connect(copied) as connection:
        before = connection.execute(
            "SELECT version, applied_at, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert migrate(str(copied)) == 96
    with sqlite3.connect(copied) as connection:
        after = connection.execute(
            "SELECT version, applied_at, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert after == before
    _assert_096_schema(copied)


def test_injected_096_failure_leaves_no_schema_or_bookkeeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "rollback-096.db"
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(95))
    assert migrate(str(db_path)) == 95

    failing = tmp_path / MIGRATION_096.name
    failing.write_text(
        MIGRATION_096.read_text(encoding="utf-8")
        + "\nINSERT INTO table_that_does_not_exist DEFAULT VALUES;\n",
        encoding="utf-8",
    )
    files = [(version, failing if version == 96 else path) for version, path in _through(96)]
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: files)

    with pytest.raises(sqlite3.OperationalError, match="table_that_does_not_exist"):
        migrate(str(db_path))

    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute("SELECT 1 FROM schema_migrations WHERE version = 96").fetchone()
            is None
        )
        for table in ("dag_step_edges", "moe_gates"):
            assert (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                is None
            )
