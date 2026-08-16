"""Apply ordered SQLite schema migrations.

H-09: the full check/apply sequence runs under one ``BEGIN IMMEDIATE`` so
concurrent startup migrators serialize instead of racing check-then-act.

M-06: applied migration file checksums are recorded (via append-only migration
068) and re-verified on every migrate. Edited-after-apply files fail closed.
Already-applied migration SQL (including 064) must never be rewritten — ship
forward-only migrations instead.

STARTUP GATE (incident 2026-08-04): the migration-file SCAN can fail (two files
claiming the same NNN prefix), and :func:`migrate_connection` is called from
REQUEST paths — every DAL that is handed a connection re-migrates on
construction. A single stray duplicate file therefore turned every request into
a 500 for as long as it sat on disk. The file inventory is now verified ONCE per
process and memoized: the API asserts it at startup (fail closed, before serving
a single request), and request-path callers reuse that verified inventory
instead of re-scanning the directory and re-raising a startup-class fault at
request time. Per-file CHECKSUMS are memoized with the cache key ``(path, dev,
ino, mtime_ns, ctime_ns, size)``. A racy-window guard does not cache files
whose mtime or ctime is within four seconds of the stat, so a same-size edit that
reuses an old mtime cannot serve a stale checksum; that read is recomputed until
the file is old enough to cache.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path

from omniagentos.contracts import default_db_path, utc_now_iso
from omniagentos.db.store import _connect

_MIGRATION_NAME = re.compile(r"^(\d{3})_.*\.sql$")

# Memoized, verified (version, path) inventory — see the module docstring.
_VERIFIED_MIGRATION_FILES: list[tuple[int, Path]] | None = None
_VERIFY_LOCK = threading.RLock()
_FILE_CHECKSUM_CACHE: dict[tuple[Path, int, int, int, int, int], str] = {}
_FILE_CHECKSUM_READ_ATTEMPTS = 5
# A stale-key collision needs the filesystem's timestamp granularity g to
# exceed this window W (two distinct revisions land in the same coarse tick
# AND that tick is still within W of "now" when the second read races the
# first's cache write). Every filesystem in use here has g <= 2s -- FAT/exFAT
# is exactly 2s, i.e. ZERO margin against a 2s window. Migration files are
# months old by the time they are read, so widening this costs nothing for
# caching (a real edit is still detected on read, just not cached instantly);
# it only removes the FAT/exFAT razor's-edge case.
_FILE_CHECKSUM_RACY_WINDOW_NS = 4_000_000_000


class MigrationChecksumMismatch(RuntimeError):
    """An applied migration file no longer matches its recorded checksum."""


class DuplicateMigrationVersion(RuntimeError):
    """Two migration files claim the same NNN version prefix.

    A RuntimeError subclass so the historical ``RuntimeError("duplicate
    migration version")`` contract still holds for existing callers/tests.
    """


class MigrationError(RuntimeError):
    """Base class for migration-apply failures that must abort the transaction."""


class MigrationForeignKeyViolation(MigrationError):
    """A migration left dangling/misfired foreign-key state right before commit.

    Copy-drop-rename (table-rebuild) migrations run inside the single
    ``BEGIN IMMEDIATE`` transaction :func:`migrate_connection` opens, and
    ``PRAGMA foreign_keys`` cannot be changed once a transaction is open -- so
    whatever enforcement state the connection had when the transaction began is
    the enforcement state for the whole migration. A ``DROP TABLE`` on a parent
    with children declared ``ON DELETE CASCADE`` can also delete rows out from
    under an unrelated child table without raising anything. Either way, this is
    the last check before the transaction becomes durable: see
    :func:`_assert_no_foreign_key_violations`.
    """


def packaged_migrations_dir() -> Path:
    """The directory the packaged ``NNN_*.sql`` migrations live in."""
    return Path(__file__).with_name("migrations")


def _scan_migration_files(migrations_dir: Path | None = None) -> list[tuple[int, Path]]:
    """Read a migrations directory and validate version uniqueness.

    ``migrations_dir`` defaults to the packaged directory; it is a parameter so
    the duplicate-version refusal can be exercised against a scratch directory
    without ever writing a stray file into the real, append-only one.
    """
    migrations_dir = migrations_dir or packaged_migrations_dir()
    found: list[tuple[int, Path]] = []
    for path in migrations_dir.glob("*.sql"):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is not None:
            found.append((int(match.group(1)), path))
    found.sort(key=lambda item: item[0])
    versions = [version for version, _ in found]
    if len(versions) != len(set(versions)):
        counts = Counter(versions)
        duplicates = sorted(version for version, count in counts.items() if count > 1)
        names = sorted(path.name for version, path in found if version in set(duplicates))
        raise DuplicateMigrationVersion(
            "duplicate migration version "
            f"{', '.join(f'{version:03d}' for version in duplicates)} "
            f"in {migrations_dir}: {', '.join(names)}"
        )
    return found


def verify_migration_files(*, refresh: bool = False) -> list[tuple[int, Path]]:
    """Return the verified migration inventory, scanning at most once per process.

    Raises :class:`DuplicateMigrationVersion` when the directory is ambiguous.
    The API calls this at STARTUP so an ambiguous directory refuses boot instead
    of 500-ing every request; after that first successful verification, request
    paths reuse the memoized result and can no longer be broken mid-flight by a
    stray file appearing on disk.

    ``refresh=True`` forces a re-scan (for the rare caller that legitimately
    changes the directory, e.g. tests); it re-raises on a duplicate and leaves the
    previously verified inventory in place, so a bad re-scan cannot poison a
    process that already verified a good one.
    """
    global _VERIFIED_MIGRATION_FILES
    with _VERIFY_LOCK:
        if _VERIFIED_MIGRATION_FILES is not None and not refresh:
            return _VERIFIED_MIGRATION_FILES
        found = _scan_migration_files()
        _VERIFIED_MIGRATION_FILES = found
        return found


def _migration_files() -> list[tuple[int, Path]]:
    """The migration inventory used by every apply path (memoized + verified)."""
    return verify_migration_files()


def _file_checksum(path: Path) -> str:
    """SHA-256 hex digest of migration bytes, cached while the file is unchanged.

    The stat identity is checked before and after reading. If a writer changes
    the file while it is being read, retry rather than associate bytes from one
    revision with metadata from another. ``_VERIFY_LOCK`` makes cache access
    safe for concurrent store construction.
    """
    for _attempt in range(_FILE_CHECKSUM_READ_ATTEMPTS):
        before = path.stat()
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_size,
        )
        key = (path, *before_identity)
        with _VERIFY_LOCK:
            cached = _FILE_CHECKSUM_CACHE.get(key)
            if cached is not None:
                return cached

            contents = path.read_bytes()
            after = path.stat()
            after_identity = (
                after.st_dev,
                after.st_ino,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_size,
            )
            if after_identity != before_identity:
                continue

            checksum = hashlib.sha256(contents).hexdigest()
            # A stamp too close to "now" cannot reliably distinguish this file
            # revision from the next one on a coarse-timestamp filesystem, so
            # do not cache it yet. The next read will recompute and, once enough
            # time has passed, cache correctly.
            if time.time_ns() - max(before.st_mtime_ns, before.st_ctime_ns) > (
                _FILE_CHECKSUM_RACY_WINDOW_NS
            ):
                _FILE_CHECKSUM_CACHE[key] = checksum
            return checksum

    raise RuntimeError(
        f"migration file changed while reading after {_FILE_CHECKSUM_READ_ATTEMPTS} attempts: "
        f"{path}"
    )


def _migration_inventory_digest(migration_files: list[tuple[int, Path]]) -> str:
    """Digest a verified migration inventory for the test harness only.

    This is never a request path. The test template builder uses the inventory
    returned by :func:`verify_migration_files`, rather than re-scanning it, so
    its cache key always matches the files a template migration actually uses.
    """
    digest = hashlib.sha256()
    for _, path in migration_files:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_file_checksum(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _iter_sql_statements(script: str) -> list[str]:
    """Split a multi-statement SQL script without relying on executescript().

    ``Connection.executescript`` issues an implicit COMMIT before running, which
    would drop the exclusive migration lock. ``sqlite3.complete_statement``
    understands comments and string literals well enough for our packaged DDL.
    """
    statements: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            statement = buf.strip()
            buf = ""
            if statement:
                statements.append(statement)
    trailing = buf.strip()
    if trailing:
        statements.append(trailing)
    return statements


def _apply_sql_script(connection: sqlite3.Connection, script: str) -> None:
    """Execute multi-statement SQL inside an already-open transaction."""
    for statement in _iter_sql_statements(script):
        connection.execute(statement)


def _schema_migrations_exists(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    return row is not None


def _checksum_column_present(connection: sqlite3.Connection) -> bool:
    if not _schema_migrations_exists(connection):
        return False
    columns = connection.execute("PRAGMA table_info(schema_migrations)").fetchall()
    for row in columns:
        name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
        if str(name) == "checksum":
            return True
    return False


def _applied_versions(connection: sqlite3.Connection) -> set[int]:
    if not _schema_migrations_exists(connection):
        return set()
    return {
        int(row["version"])
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }


def _applied_checksums(connection: sqlite3.Connection) -> dict[int, str | None]:
    """Map applied version → recorded checksum (None if column/value absent)."""
    if not _schema_migrations_exists(connection):
        return {}
    if _checksum_column_present(connection):
        return {
            int(row["version"]): (None if row["checksum"] is None else str(row["checksum"]))
            for row in connection.execute(
                "SELECT version, checksum FROM schema_migrations"
            ).fetchall()
        }
    return {
        int(row["version"]): None
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }


def _record_applied(
    connection: sqlite3.Connection,
    version: int,
    timestamp: str,
    checksum: str,
) -> None:
    """Insert a schema_migrations row, including checksum when the column exists."""
    if _checksum_column_present(connection):
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at, checksum) VALUES (?, ?, ?)",
            (version, timestamp, checksum),
        )
    else:
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, timestamp),
        )


def _verify_and_backfill_checksums(
    connection: sqlite3.Connection,
    migration_files: list[tuple[int, Path]],
) -> None:
    """Fail closed on edited applied files; backfill NULL checksums once.

    Backfill uses the current on-disk bytes (first trust after migration 068).
    Subsequent migrates require an exact match — that is how post-apply edits
    of frozen files like 064 become visible.
    """
    if not _checksum_column_present(connection):
        return

    for version, path in migration_files:
        row = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = ?",
            (version,),
        ).fetchone()
        if row is None:
            continue
        recorded = row["checksum"] if isinstance(row, sqlite3.Row) else row[0]
        disk = _file_checksum(path)
        if recorded is None:
            connection.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = ? AND checksum IS NULL",
                (disk, version),
            )
            continue
        if str(recorded) != disk:
            raise MigrationChecksumMismatch(
                f"migration {version:03d} ({path.name}) was edited after application: "
                f"recorded={recorded} disk={disk}"
            )


def _foreign_key_check_row(row: sqlite3.Row | tuple[object, ...]) -> tuple[object, object, object, object]:
    """Normalize one ``PRAGMA foreign_key_check`` row to (table, rowid, parent, fkid)."""
    if isinstance(row, sqlite3.Row):
        return (row["table"], row["rowid"], row["parent"], row["fkid"])
    return (row[0], row[1], row[2], row[3])


def _assert_no_foreign_key_violations(
    connection: sqlite3.Connection,
    newly_applied: list[tuple[int, Path]],
) -> None:
    """Refuse to commit a migration that left dangling/misfired FK state.

    Register-10 sect. 6 item 14: table-rebuild (copy-drop-rename) migrations
    run inside the one ``BEGIN IMMEDIATE`` transaction that
    :func:`migrate_connection` opens, where ``PRAGMA foreign_keys`` cannot be
    toggled -- whatever enforcement state held when the transaction began holds
    for the whole migration. Live enforcement stops most naive breakage, but it
    does not stop everything (a connection that never enabled ``foreign_keys``
    before this transaction opened enforces nothing at all, and ``ON DELETE
    CASCADE`` can quietly delete unrelated child rows without raising). This is
    the backstop: run once, right before commit, against the FINAL state of
    every migration this call applied -- never per statement, so a chain of
    already-applied (no-op) migrate_connection() calls pays nothing for it.
    """
    if not newly_applied:
        return
    violations = [
        _foreign_key_check_row(row) for row in connection.execute("PRAGMA foreign_key_check")
    ]
    if not violations:
        return
    applied_names = ", ".join(f"{version:03d} ({path.name})" for version, path in newly_applied)
    raise MigrationForeignKeyViolation(
        f"PRAGMA foreign_key_check found {len(violations)} dangling/misfired foreign "
        f"key row(s) immediately before commit, after applying migration(s) "
        f"{applied_names}. Violations (table, rowid, parent, fkid): {violations!r}. "
        "Likely cause: child-rebuild order or CASCADE misfire -- a copy-drop-rename "
        "table rebuild in one of the migrations above left a child row pointing at a "
        "parent row that no longer exists, or an ON DELETE CASCADE fired across "
        "children during a DROP TABLE. Fix the migration named above: rebuild/re-point "
        "children in the same transaction as their parent (children before parents "
        "when dropping, per migrations like 119/120), and re-verify with this check."
    )


def migrate_connection(connection: sqlite3.Connection) -> int:
    """Apply pending migrations on an existing connection.

    The version check, checksum verification, apply, and bookkeeping all run
    under one exclusive ``BEGIN IMMEDIATE`` so concurrent callers serialize the
    full sequence (H-09) rather than racing check-then-act.

    This is public for :class:`SqliteStore` initialization, including in-memory
    databases where opening a second connection would create a different DB.
    """
    # Exclusive writer lock for the whole check/apply sequence. Connections use
    # isolation_level=None (autocommit), so transactions are fully manual.
    connection.execute("BEGIN IMMEDIATE")
    try:
        migration_files = _migration_files()
        applied = _applied_checksums(connection)
        newly_applied: list[tuple[int, Path]] = []

        for version, path in migration_files:
            disk_checksum = _file_checksum(path)
            if version in applied:
                recorded = applied[version]
                if recorded is not None and recorded != disk_checksum:
                    raise MigrationChecksumMismatch(
                        f"migration {version:03d} ({path.name}) was edited after "
                        f"application: recorded={recorded} disk={disk_checksum}"
                    )
                continue

            script = path.read_text(encoding="utf-8")
            _apply_sql_script(connection, script)
            _record_applied(connection, version, utc_now_iso(), disk_checksum)
            # After 068 lands mid-run, subsequent inserts include checksums.
            applied[version] = disk_checksum if _checksum_column_present(connection) else None
            newly_applied.append((version, path))

        _verify_and_backfill_checksums(connection, migration_files)
        _assert_no_foreign_key_violations(connection, newly_applied)
        connection.commit()
        return max(applied, default=0)
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def migrate(db_path: str) -> int:
    """Apply all packaged migrations and return the final schema version."""
    connection = _connect(db_path)
    try:
        return migrate_connection(connection)
    finally:
        connection.close()


def _main() -> None:
    parser = argparse.ArgumentParser(description="Apply OmniAgentOS database migrations")
    parser.add_argument("db_path", nargs="?", default=default_db_path())
    args = parser.parse_args()
    print(migrate(args.db_path))


if __name__ == "__main__":
    _main()
