"""The queue DB stays out of the packaged migration set (SPEC §1.2).

`omniagentos/db/migrate.py` raises DuplicateMigrationVersion when two files claim
one NNN prefix, and its own docstring records what that cost: "a single stray
duplicate file therefore turned every request into a 500 for as long as it sat on
disk". Constructing a SqliteStore against var/workqueue.sqlite3 would apply all
121 packaged migrations here and hand back exactly that collision. This test is
the standing check that we did not.
"""

from __future__ import annotations

import sqlite3

from omniagentos.workqueue.migrate import migrate_queue_connection, migrations_dir
from omniagentos.workqueue.store import WorkQueueStore


def _tables(db_path) -> set[str]:
    connection = sqlite3.connect(str(db_path))
    try:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        connection.close()


def test_the_queue_db_contains_only_wq_tables(db_path):
    store = WorkQueueStore(db_path)
    store.close()

    tables = _tables(db_path)
    # sqlite_sequence is SQLite's own bookkeeping for AUTOINCREMENT.
    assert tables - {"sqlite_sequence"} == {
        "schema_migrations",
        "wq_units",
        "wq_attempts",
        "wq_machines",
        "wq_workers",
        "wq_refusals",
    }


def test_migration_is_idempotent_and_records_a_checksum(db_path):
    first = WorkQueueStore(db_path)
    first.close()

    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
        assert [row["version"] for row in rows] == [1, 2]
        assert all(row["checksum"] for row in rows)
        # Reopening applies nothing: the second store must not re-run either one.
        assert migrate_queue_connection(connection) == 0
    finally:
        connection.close()

    second = WorkQueueStore(db_path)
    try:
        assert second.status()["depth"]["queued"] == 0
    finally:
        second.close()


def test_migrations_dir_is_the_queues_own():
    directory = migrations_dir()
    assert directory.name == "migrations"
    assert directory.parent.name == "workqueue"
    # The queue DB has its OWN sequence. 002 here is machine roles (D8a) and has
    # nothing to do with the packaged 002 — the whole reason this directory
    # exists is that the two numbering spaces must never meet.
    assert [path.name for path in sorted(directory.glob("*.sql"))] == [
        "001_workqueue.sql",
        "002_machine_roles.sql",
    ]
