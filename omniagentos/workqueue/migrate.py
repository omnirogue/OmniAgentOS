"""Migrator for the work queue's OWN database (var/workqueue.sqlite3).

Why this file exists at all, given ``omniagentos/db/migrate.py`` is right there:
``migrate_connection()`` is hardcoded to the packaged migrations directory, and
running it against the queue DB would apply all 121 packaged migrations into a
file whose entire purpose is to stay out of their numbering space
(SPEC-shared-queue §1.2 — "a single stray duplicate file therefore turned every
request into a 500"). So we reuse the three helpers that DO take a directory
parameter — ``_scan_migration_files``, ``_iter_sql_statements`` (via
``_apply_sql_script``) — and keep the ``schema_migrations(version, applied_at,
checksum)`` shape identical, and nothing else.

Concurrency: the whole check/apply sequence runs under one ``BEGIN IMMEDIATE``,
so N stores opening the same fresh DB at once serialize instead of racing
check-then-act. The loser sees 001 already applied and does nothing.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from omniagentos.db.migrate import _apply_sql_script, _scan_migration_files
from omniagentos.workqueue.schema import utc_now_iso

__all__ = ["migrations_dir", "migrate_queue_connection"]


def migrations_dir() -> Path:
    """The queue's own ``NNN_*.sql`` directory — never the packaged one."""
    return Path(__file__).with_name("migrations")


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _applied_versions(connection: sqlite3.Connection) -> set[int]:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if row is None:
        return set()
    return {
        int(r[0]) for r in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }


def migrate_queue_connection(connection: sqlite3.Connection) -> int:
    """Apply pending queue migrations on an open connection. Returns how many ran."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        applied = _applied_versions(connection)
        ran = 0
        for version, path in _scan_migration_files(migrations_dir()):
            if version in applied:
                continue
            # The 001 script creates schema_migrations itself, so the bookkeeping
            # row can only be written after the script has run.
            _apply_sql_script(connection, path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at, checksum) VALUES (?, ?, ?)",
                (version, utc_now_iso(), _checksum(path)),
            )
            ran += 1
        connection.execute("COMMIT")
        return ran
    except BaseException:
        connection.execute("ROLLBACK")
        raise
