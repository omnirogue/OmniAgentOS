"""The template helper must be indistinguishable from a real migration.

A speed optimisation that quietly changes the schema tests run against is worse
than the slowness it replaces, so this proves equivalence rather than asserting
it: schema objects, applied-migration rows, row counts, and the
checksum-mismatch contract all have to match a from-scratch build.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.db import migrate as migrate_module
from omniagentos.db.store import SqliteStore, _connect
from tests.support import db_template
from tests.support.db_template import make_store, migrated_db, template_for


def _snapshot(db_path: str) -> tuple[list[tuple], list[int], dict[str, int]]:
    connection = sqlite3.connect(db_path)
    try:
        schema = sorted(connection.execute("SELECT type, name, sql FROM sqlite_master").fetchall())
        versions = sorted(
            int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")
        )
        counts = {}
        for (table,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall():
            if table.startswith("sqlite_"):
                continue
            counts[table] = connection.execute(f"SELECT count(*) FROM '{table}'").fetchone()[0]
        return schema, versions, counts
    finally:
        connection.close()


def test_template_copy_matches_a_fresh_migration(tmp_path: Path) -> None:
    fresh_path = str(tmp_path / "fresh.db")
    SqliteStore(fresh_path).close()

    copy_path = migrated_db(SqliteStore, tmp_path / "copy.db")
    SqliteStore(copy_path).close()

    fresh_schema, fresh_versions, fresh_counts = _snapshot(fresh_path)
    copy_schema, copy_versions, copy_counts = _snapshot(copy_path)

    assert copy_schema == fresh_schema
    assert copy_versions == fresh_versions
    assert copy_versions, "template carries no applied migrations"
    assert copy_counts == fresh_counts


def test_template_is_not_shared_between_callers(tmp_path: Path) -> None:
    """Each caller gets its own file; writes must not reach the template."""
    first = make_store(SqliteStore, tmp_path / "one.db")
    second = make_store(SqliteStore, tmp_path / "two.db")
    try:
        first._connection.execute(
            "INSERT INTO events (ts, type, actor, action) VALUES (?, ?, ?, ?)",
            ("2026-01-01T00:00:00Z", "t", "user", "a"),
        )
        assert first._connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert second._connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    finally:
        first.close()
        second.close()

    template = template_for(SqliteStore)
    connection = sqlite3.connect(str(template))
    try:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    finally:
        connection.close()


def test_template_is_reused_across_store_classes_independently(tmp_path: Path) -> None:
    assert template_for(SqliteStore) == template_for(SqliteStore)
    assert template_for(CollabStore) != template_for(SqliteStore)
    collab = CollabStore(migrated_db(CollabStore, tmp_path / "collab.db"))
    try:
        assert collab._connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[
            0
        ] == len(_snapshot(str(template_for(CollabStore)))[1])
    finally:
        collab._store.close()


def test_template_rebuilds_when_migration_inventory_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long-lived test process must not retain an old-schema template."""
    current_digest = {"value": "first"}
    calls: list[Path] = []
    original_make_copy_safe = db_template._make_copy_safe

    def counted_make_copy_safe(path: Path) -> None:
        calls.append(path)
        original_make_copy_safe(path)

    monkeypatch.setattr(db_template, "_TEMPLATES", {})
    monkeypatch.setattr(
        db_template,
        "_migration_inventory_digest",
        lambda _migration_files: current_digest["value"],
    )
    monkeypatch.setattr(db_template, "_make_copy_safe", counted_make_copy_safe)

    template_for(SqliteStore)
    template_for(SqliteStore)
    current_digest["value"] = "second"
    template_for(SqliteStore)
    template_for(SqliteStore)

    assert len(calls) == 2


def test_template_retries_when_inventory_moves_mid_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration edit during a template build retries with the new inventory."""
    current_digest = {"value": "first"}
    builds: list[Path] = []
    original_make_copy_safe = db_template._make_copy_safe

    def inventory_digest(_migration_files: list[tuple[int, Path]]) -> str:
        return current_digest["value"]

    def move_inventory_after_first_build(path: Path) -> None:
        original_make_copy_safe(path)
        builds.append(path)
        if len(builds) == 1:
            current_digest["value"] = "second"

    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    monkeypatch.setattr(db_template, "_TEMPLATES", {})
    monkeypatch.setattr(db_template, "_TEMPLATE_DIR", template_dir)
    monkeypatch.setattr(db_template, "_migration_inventory_digest", inventory_digest)
    monkeypatch.setattr(db_template, "_make_copy_safe", move_inventory_after_first_build)

    template_for(SqliteStore)

    assert len(builds) == 2


def test_template_rebuilds_from_a_real_migrations_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding a real migration rebuilds the template and changes the next copy's schema."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    initial = migrate_module.packaged_migrations_dir() / "001_init.sql"
    (migrations_dir / initial.name).write_bytes(initial.read_bytes())

    def migration_files() -> list[tuple[int, Path]]:
        return migrate_module._scan_migration_files(migrations_dir)

    class ScratchMigrationStore:
        def __init__(self, db_path: str) -> None:
            self.connection = _connect(db_path)
            migrate_module.migrate_connection(self.connection)

        def close(self) -> None:
            self.connection.close()

    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    monkeypatch.setattr(db_template, "_TEMPLATES", {})
    monkeypatch.setattr(db_template, "_TEMPLATE_DIR", template_dir)
    monkeypatch.setattr(db_template, "verify_migration_files", lambda *, refresh=False: migration_files())
    monkeypatch.setattr(migrate_module, "_migration_files", migration_files)

    migrated_db(ScratchMigrationStore, tmp_path / "before.db")
    (migrations_dir / "002_template_rebuild.sql").write_text(
        "CREATE TABLE template_rebuild_coverage (id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )
    rebuilt = migrated_db(ScratchMigrationStore, tmp_path / "after.db")

    with sqlite3.connect(rebuilt) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'template_rebuild_coverage'"
        ).fetchone() == ("template_rebuild_coverage",)


def test_template_build_refuses_to_cache_when_store_applies_a_different_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A monkeypatched ``migrate._migration_files`` must not poison a full-inventory key.

    ``verify_migration_files(refresh=True)`` (used for the digest) and
    ``migrate._migration_files()`` (used by the store constructor's
    ``migrate_connection``) normally read the same directory. Some tests
    (e.g. tests/swarm/test_effort_levels.py) monkeypatch the latter directly
    to a truncated prefix, which desynchronises the two: without the
    applied-count post-condition, the truncated schema would get cached under
    the full inventory's digest and every later caller of that template would
    silently receive a partial database.
    """
    all_files = migrate_module._migration_files()
    truncated = [(version, path) for version, path in all_files if version <= 39]
    assert 0 < len(truncated) < len(all_files)

    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    monkeypatch.setattr(db_template, "_TEMPLATES", {})
    monkeypatch.setattr(db_template, "_TEMPLATE_DIR", template_dir)
    monkeypatch.setattr(migrate_module, "_migration_files", lambda: truncated)

    with pytest.raises(RuntimeError, match="applied .* migrations but the verified inventory"):
        template_for(SqliteStore)

    # The refused build must not have left a cached, poisoned template entry.
    assert db_template._TEMPLATES == {}


def test_migrated_db_rejects_memory_databases() -> None:
    with pytest.raises(ValueError, match="file path"):
        migrated_db(SqliteStore, ":memory:")


def test_checksum_verification_still_runs_on_a_template_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """migrate.py's edited-after-apply guard must not be bypassed by the copy."""
    from omniagentos.db import migrate as migrate_module

    db_path = migrated_db(SqliteStore, tmp_path / "checksum.db")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("UPDATE schema_migrations SET checksum = 'bogus' WHERE version = 1")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(migrate_module.MigrationChecksumMismatch):
        SqliteStore(db_path)
