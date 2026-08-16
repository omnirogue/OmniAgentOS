from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate


def _migrate_through(
    version: int,
    db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    packaged = _migration_files()
    subset = [(number, path) for number, path in packaged if number <= version]
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: subset)
    return migrate(str(db_path))


def test_fresh_migration_adds_provider_call_usage(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"

    assert migrate(str(db_path)) >= 92

    with sqlite3.connect(db_path) as connection:
        table = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'provider_call_usage'"
        ).fetchone()
        checksum = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 92"
        ).fetchone()

    assert table is not None
    assert checksum is not None and checksum[0]


def test_upgrade_from_pre_092_preserves_schema_and_adds_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "upgrade.db"
    packaged = _migration_files()
    assert any(version == 92 for version, _ in packaged)
    assert _migrate_through(91, db_path, monkeypatch) == 91

    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE pre_092_sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO pre_092_sentinel VALUES ('preserved')")
        connection.commit()

    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: packaged)
    assert migrate(str(db_path)) >= 92

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT value FROM pre_092_sentinel").fetchone() == ("preserved",)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'provider_call_usage'"
        ).fetchone() == (1,)


def test_provider_call_migration_replay_is_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "replay.db"
    assert migrate(str(db_path)) >= 92

    with sqlite3.connect(db_path) as connection:
        before = connection.execute(
            "SELECT version, applied_at, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()

    assert migrate(str(db_path)) >= 92

    with sqlite3.connect(db_path) as connection:
        after = connection.execute(
            "SELECT version, applied_at, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert after == before


def test_provider_call_schema_has_unique_id_and_cost_quality_columns(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "shape.db"
    migrate(str(db_path))

    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_call_usage)")}
        unique_indexes = [
            row for row in connection.execute("PRAGMA index_list(provider_call_usage)") if row[2]
        ]
        unique_columns = {
            tuple(
                info[2] for info in connection.execute(f"PRAGMA index_info({index[1]})").fetchall()
            )
            for index in unique_indexes
        }

    assert {
        "cost_usd_decimal",
        "cost_usd_nanos",
        "cost_upper_bound_usd_nanos",
        "cost_quality",
        "cost_source",
    } <= columns
    assert ("call_id",) in unique_columns
