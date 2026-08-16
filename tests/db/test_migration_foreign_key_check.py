"""PRAGMA foreign_key_check backstop before every migration commit.

Register-10 sect. 6 item 14: copy-drop-rename (table-rebuild) migrations run
inside the single ``BEGIN IMMEDIATE`` transaction ``migrate_connection()``
opens, where ``PRAGMA foreign_keys`` cannot be toggled once that transaction
is open -- whatever enforcement state the connection had when the transaction
began holds for the whole migration. Live enforcement stops most naive
breakage, but a connection that never turned ``foreign_keys`` on before
``migrate_connection()`` was called enforces nothing at all for the whole
migration, and ``ON DELETE CASCADE`` can quietly delete unrelated child rows
without ever raising.

These tests build a synthetic fixture migration -- on top of real migration
001 (which supplies ``schema_migrations``) -- that rebuilds a parent table
without re-pointing a child, and pin that ``migrate_connection()`` refuses to
commit it, leaving the database at its pre-migration version.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import (
    MigrationForeignKeyViolation,
    _migration_files,
    migrate,
    migrate_connection,
)

_BOOTSTRAP_SQL = """
CREATE TABLE parent_fixture(id TEXT PRIMARY KEY);
CREATE TABLE child_fixture(
  id INTEGER PRIMARY KEY,
  parent_id TEXT NOT NULL REFERENCES parent_fixture(id)
);
INSERT INTO parent_fixture(id) VALUES ('p1'), ('p2');
INSERT INTO child_fixture(id, parent_id) VALUES (1, 'p1'), (2, 'p2');
"""

# The defect class register-10 sect. 6 item 14 names: parent_fixture is
# rebuilt copy-drop-rename style and drops row 'p1' along the way, but
# child_fixture (which still has a row referencing 'p1') is never rebuilt or
# re-pointed. child_fixture's row 1 is left dangling.
_BROKEN_REBUILD_SQL = """
CREATE TABLE parent_fixture_new(id TEXT PRIMARY KEY);
INSERT INTO parent_fixture_new(id) SELECT id FROM parent_fixture WHERE id != 'p1';
DROP TABLE parent_fixture;
ALTER TABLE parent_fixture_new RENAME TO parent_fixture;
"""

# A same-shaped, but harmless, 901 for the companion no-false-positive check.
_CLEAN_REBUILD_SQL = """
CREATE TABLE parent_fixture_new(id TEXT PRIMARY KEY);
INSERT INTO parent_fixture_new(id) SELECT id FROM parent_fixture;
DROP TABLE child_fixture;
DROP TABLE parent_fixture;
ALTER TABLE parent_fixture_new RENAME TO parent_fixture;
CREATE TABLE child_fixture(
  id INTEGER PRIMARY KEY,
  parent_id TEXT NOT NULL REFERENCES parent_fixture(id)
);
INSERT INTO child_fixture(id, parent_id) VALUES (1, 'p1'), (2, 'p2');
"""


def _base_migration_001() -> list[tuple[int, Path]]:
    return [(version, path) for version, path in _migration_files() if version == 1]


def _bootstrap_migration(tmp_path: Path) -> tuple[int, Path]:
    path = tmp_path / "900_fixture_bootstrap.sql"
    path.write_text(_BOOTSTRAP_SQL, encoding="utf-8")
    return (900, path)


def _rebuild_migration(tmp_path: Path, *, broken: bool) -> tuple[int, Path]:
    path = tmp_path / "901_fixture_rebuild.sql"
    path.write_text(_BROKEN_REBUILD_SQL if broken else _CLEAN_REBUILD_SQL, encoding="utf-8")
    return (901, path)


def test_broken_parent_rebuild_is_refused_and_leaves_pre_migration_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration that orphans a child row must never commit."""
    db_path = tmp_path / "fk-violation.db"
    bootstrap_files = [*_base_migration_001(), _bootstrap_migration(tmp_path)]

    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: bootstrap_files)
    assert migrate(str(db_path)) == 900

    with sqlite3.connect(db_path) as connection:
        pre_parents = connection.execute("SELECT id FROM parent_fixture ORDER BY id").fetchall()
        pre_children = connection.execute(
            "SELECT id, parent_id FROM child_fixture ORDER BY id"
        ).fetchall()

    broken_files = [*bootstrap_files, _rebuild_migration(tmp_path, broken=True)]
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: broken_files)

    # migrate_connection() is exercised directly on a raw connection (never
    # routed through _connect(), so PRAGMA foreign_keys was never turned on)
    # -- it cannot change mid-transaction, so this reproduces exactly the
    # scenario register-10 sect. 6 item 14 describes: live enforcement never
    # engages, and PRAGMA foreign_key_check is the only thing standing between
    # the bad rebuild and a committed, silently-corrupt database. row_factory
    # is set (migrate_connection()'s bookkeeping queries require it, same as
    # every real caller via _connect()) but foreign_keys is deliberately left
    # at SQLite's default (off).
    connection = sqlite3.connect(str(db_path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        with pytest.raises(
            MigrationForeignKeyViolation,
            match=r"901 \(901_fixture_rebuild\.sql\).*child-rebuild order or CASCADE misfire",
        ):
            migrate_connection(connection)
    finally:
        connection.close()

    # The transaction must have rolled back entirely: version stays at 900,
    # and the pre-migration parent/child rows are untouched (not partially
    # rebuilt, not orphaned).
    with sqlite3.connect(db_path) as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        post_parents = connection.execute("SELECT id FROM parent_fixture ORDER BY id").fetchall()
        post_children = connection.execute(
            "SELECT id, parent_id FROM child_fixture ORDER BY id"
        ).fetchall()

    assert version == 900
    assert post_parents == pre_parents
    assert post_children == pre_children


def test_clean_rebuild_of_the_same_shape_still_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new check must not false-positive on a correctly-ordered rebuild."""
    db_path = tmp_path / "fk-clean.db"
    bootstrap_files = [*_base_migration_001(), _bootstrap_migration(tmp_path)]
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: bootstrap_files)
    assert migrate(str(db_path)) == 900

    clean_files = [*bootstrap_files, _rebuild_migration(tmp_path, broken=False)]
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: clean_files)
    assert migrate(str(db_path)) == 901

    with sqlite3.connect(db_path) as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        children = connection.execute(
            "SELECT id, parent_id FROM child_fixture ORDER BY id"
        ).fetchall()
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert version == 901
    assert children == [(1, "p1"), (2, "p2")]
    assert violations == []
