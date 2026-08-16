from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from omniagentos.db.migrate import (
    _FILE_CHECKSUM_CACHE,
    MigrationChecksumMismatch,
    _file_checksum,
    _migration_files,
    migrate,
    migrate_connection,
)
from omniagentos.db.store import SqliteStore, _connect, _enable_wal

# Derive the expected final schema version from the migration files on disk so
# these assertions don't go stale each time a new NNN_*.sql migration lands.
LATEST_VERSION = max(version for version, _ in _migration_files())


def _checksum_cache_key(path: Path) -> tuple[Path, int, int, int, int, int]:
    metadata = path.stat()
    return (
        path,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_size,
    )


def test_file_checksum_cache_invalidates_when_file_metadata_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "migration.sql"
    path.write_text("SELECT 1;", encoding="utf-8")
    real_time_ns = time.time_ns
    monkeypatch.setattr(
        "omniagentos.db.migrate.time.time_ns",
        lambda: real_time_ns() + 5_000_000_000,
    )

    original = _file_checksum(path)
    assert _file_checksum(path) == original
    assert _checksum_cache_key(path) in _FILE_CHECKSUM_CACHE

    path.write_text("SELECT 1;\n-- changed", encoding="utf-8")
    changed = _file_checksum(path)

    assert changed != original
    assert _checksum_cache_key(path) in _FILE_CHECKSUM_CACHE


class _StatOnceChangedPath:
    """Duck-typed ``Path`` proxy: one mutated ``stat()``, then the real one.

    Used instead of ``monkeypatch.setattr("omniagentos.db.migrate.Path.stat",
    ...)`` -- patching the ``Path`` class patches ``stat()`` for every
    ``Path`` instance in the process for the test's duration, which is a
    correctness risk for anything else running concurrently (xdist workers,
    pytest's own bookkeeping). A proxy that implements only what
    ``_file_checksum`` uses (``stat()``, ``read_bytes()``, hashing/equality
    for its cache-key use) cannot affect any object but itself.
    """

    def __init__(self, real: Path) -> None:
        self._real = real
        self.retried = False

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        result = self._real.stat(follow_symlinks=follow_symlinks)
        if not self.retried:
            self.retried = True
            changed = list(result)
            changed[stat.ST_SIZE] += 1
            return os.stat_result(changed)
        return result

    def read_bytes(self) -> bytes:
        return self._real.read_bytes()

    def __eq__(self, other: object) -> bool:
        return self._real == getattr(other, "_real", other)

    def __hash__(self) -> int:
        return hash(self._real)

    def __repr__(self) -> str:
        return f"_StatOnceChangedPath({self._real!r})"


def test_file_checksum_retries_when_stat_changes_during_read(tmp_path: Path) -> None:
    """A stat that mutates mid-read must be retried, not trusted, and not leak.

    The assertion is behavioural (a retry actually happened and the correct
    checksum was returned) rather than coupled to an exact call count, so a
    later change to the retry loop's internals does not need to keep this
    test's arithmetic in sync.
    """
    real_path = tmp_path / "migration.sql"
    real_path.write_text("SELECT 1;", encoding="utf-8")
    proxy = _StatOnceChangedPath(real_path)

    assert _file_checksum(proxy) == hashlib.sha256(real_path.read_bytes()).hexdigest()  # type: ignore[arg-type]
    assert proxy.retried, "the mid-read stat change was never exercised"


def test_initial_migration_is_verbatim() -> None:
    root = Path(__file__).parents[2]
    assert (root / "contracts/schema.sql").read_bytes() == (
        root / "omniagentos/db/migrations/001_init.sql"
    ).read_bytes()


def test_migrate_twice_is_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "store.db"
    assert migrate(str(db_path)) == LATEST_VERSION
    with sqlite3.connect(db_path) as connection:
        first = connection.execute("SELECT version, applied_at FROM schema_migrations").fetchall()

    assert migrate(str(db_path)) == LATEST_VERSION
    with sqlite3.connect(db_path) as connection:
        second = connection.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
    assert second == first


def test_migration_module_is_runnable(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.db"
    result = subprocess.run(
        [sys.executable, "-m", "omniagentos.db.migrate", str(db_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == str(LATEST_VERSION)


def test_store_construction_migrates_fresh_database(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh" / "store.db"

    store = SqliteStore(str(db_path))

    assert store.list_runs({}, limit=1) == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (
            LATEST_VERSION,
        )


def test_checksums_recorded_for_all_applied_migrations(tmp_path: Path) -> None:
    """M-06: every applied version gets a durable SHA-256 of the migration file."""
    db_path = tmp_path / "checksums.db"
    assert migrate(str(db_path)) == LATEST_VERSION

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()

    by_version = {int(version): checksum for version, checksum in rows}
    assert LATEST_VERSION in by_version
    assert 68 in by_version, "migration 068 must land the checksum column"

    for version, path in _migration_files():
        assert version in by_version
        recorded = by_version[version]
        assert recorded is not None, f"version {version} missing checksum"
        assert recorded == hashlib.sha256(path.read_bytes()).hexdigest(), (
            f"version {version} checksum mismatch"
        )


def _copy_migration_files(tmp_path: Path) -> list[tuple[int, Path]]:
    destination = tmp_path / "migration-files"
    destination.mkdir()
    copied: list[tuple[int, Path]] = []
    for version, source in _migration_files():
        target = destination / source.name
        target.write_bytes(source.read_bytes())
        copied.append((version, target))
    return copied


def test_edited_applied_migration_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-06: a real edit to an applied migration fails closed on next migrate."""
    migration_files = _copy_migration_files(tmp_path)
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: migration_files)
    db_path = tmp_path / "edited.db"
    migrate(str(db_path))

    target = next(path for version, path in migration_files if version == 64)
    target.write_bytes(target.read_bytes() + b"\n-- post-application edit\n")

    with pytest.raises(MigrationChecksumMismatch, match=r"064.*edited after application"):
        migrate(str(db_path))


def test_edited_applied_migration_with_restored_mtime_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-size edit cannot reuse a stale checksum after ``utime`` restores mtime."""
    migration_files = _copy_migration_files(tmp_path)
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: migration_files)
    db_path = tmp_path / "edited-restored-mtime.db"
    migrate(str(db_path))

    target = next(path for version, path in migration_files if version == 64)
    original_stat = target.stat()
    original = target.read_bytes()
    changed = original.replace(b"Phase A", b"Phase B", 1)
    assert len(changed) == len(original)
    target.write_bytes(changed)
    os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    with pytest.raises(MigrationChecksumMismatch, match=r"064.*edited after application"):
        migrate(str(db_path))


def test_ino_ctime_key_pins_edited_applied_migration_even_when_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the (dev, ino, mtime_ns, ctime_ns, size) cache key on its own.

    A four-way ablation (Opus xhigh review) showed
    ``test_edited_applied_migration_with_restored_mtime_is_detected`` passes
    with EITHER half of the fix alone, because
    ``_copy_migration_files`` always produces fresh files whose checksum is
    never actually cached before the edit -- so that test never proves the
    cache key itself catches a same-size, same-mtime edit of an ENTRY THAT IS
    ALREADY CACHED. This test forces the entry to cache first (advance the
    clock past the racy window so the initial read is eligible to cache),
    then performs the same-size edit with ``utime``-restored mtime, and
    asserts the mismatch still fires. Removing the ino/ctime component of the
    cache key (reverting to ``(path, mtime_ns, size)``) makes this fail.
    """
    migration_files = _copy_migration_files(tmp_path)
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: migration_files)
    db_path = tmp_path / "edited-cached.db"
    migrate(str(db_path))

    target = next(path for version, path in migration_files if version == 64)
    real_time_ns = time.time_ns
    monkeypatch.setattr(
        "omniagentos.db.migrate.time.time_ns",
        lambda: real_time_ns() + 5_000_000_000,
    )
    original_stat = target.stat()
    original = target.read_bytes()

    # Force this file's checksum into the cache (past the racy window).
    _file_checksum(target)
    assert _checksum_cache_key(target) in _FILE_CHECKSUM_CACHE

    changed = original.replace(b"Phase A", b"Phase B", 1)
    assert len(changed) == len(original)
    target.write_bytes(changed)
    os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    with pytest.raises(MigrationChecksumMismatch, match=r"064.*edited after application"):
        migrate(str(db_path))


def test_racy_window_does_not_cache_a_freshly_written_file(tmp_path: Path) -> None:
    """Pins the racy-window guard on its own (see the ablation note above).

    A file whose mtime/ctime is within the racy window of "now" must not be
    cached at all, regardless of the cache key's identity fields.
    """
    path = tmp_path / "fresh.sql"
    path.write_text("SELECT 1;", encoding="utf-8")

    _file_checksum(path)

    assert _checksum_cache_key(path) not in _FILE_CHECKSUM_CACHE


def test_checksum_mismatch_rolls_back_and_leaves_db_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "usable.db"
    migrate(str(db_path))

    real_checksum = _file_checksum

    def forged_checksum(path: Path) -> str:
        if path.name.startswith("001_"):
            return "f" * 64
        return real_checksum(path)

    monkeypatch.setattr("omniagentos.db.migrate._file_checksum", forged_checksum)
    with pytest.raises(MigrationChecksumMismatch):
        migrate(str(db_path))

    # After a failed check the connection is closed; a clean migrate with real
    # checksums must still succeed (no half-applied bookkeeping).
    monkeypatch.undo()
    assert migrate(str(db_path)) == LATEST_VERSION


def test_concurrent_migrations_serialize_without_crash(tmp_path: Path) -> None:
    """H-09: concurrent startup migrators must not race check-then-act."""
    db_path = tmp_path / "concurrent.db"
    workers = 8

    def _run() -> int:
        return migrate(str(db_path))

    versions: list[int] = []
    errors: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run) for _ in range(workers)]
        for future in as_completed(futures):
            try:
                versions.append(future.result())
            except BaseException as exc:  # noqa: BLE001 — collect for assertion
                errors.append(exc)

    assert errors == [], f"concurrent migrate raised: {errors!r}"
    assert versions == [LATEST_VERSION] * workers

    with sqlite3.connect(db_path) as connection:
        applied = {
            int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")
        }
        checksums = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE checksum IS NULL OR checksum = ''"
        ).fetchone()
    assert applied == {version for version, _ in _migration_files()}
    assert checksums == (0,)


def test_concurrent_migrate_connection_on_shared_file(tmp_path: Path) -> None:
    """H-09: separate same-process connections still serialize."""
    db_path = str(tmp_path / "shared.db")
    # Seed an empty file so every worker opens the same path.
    Path(db_path).touch()

    def _run() -> int:
        connection = _connect(db_path)
        try:
            return migrate_connection(connection)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: _run(), range(6)))

    assert results == [LATEST_VERSION] * 6
    assert migrate(db_path) == LATEST_VERSION


def test_concurrent_migration_processes_serialize(tmp_path: Path) -> None:
    """H-09: independent processes can concurrently migrate one fresh DB."""
    db_path = str(tmp_path / "processes.db")
    start_at = time.time() + 0.75
    worker = (
        "import sys, time\n"
        "from omniagentos.db.migrate import migrate\n"
        "time.sleep(max(0.0, float(sys.argv[2]) - time.time()))\n"
        "print(migrate(sys.argv[1]), flush=True)\n"
    )

    def _run_process() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", worker, db_path, str(start_at)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _run_process(), range(8)))

    failures = [
        (result.returncode, result.stdout, result.stderr)
        for result in results
        if result.returncode != 0 or result.stdout.strip() != str(LATEST_VERSION)
    ]
    assert failures == []

    with sqlite3.connect(db_path) as connection:
        versions = connection.execute(
            "SELECT version, COUNT(*) FROM schema_migrations GROUP BY version HAVING COUNT(*) != 1"
        ).fetchall()
        missing_checksums = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE checksum IS NULL OR checksum = ''"
        ).fetchone()
    assert versions == []
    assert missing_checksums == (0,)


def test_migration_process_waits_for_wal_handshake_lock(tmp_path: Path) -> None:
    """H-09: startup contention before BEGIN IMMEDIATE is also bounded/retried."""
    db_path = str(tmp_path / "wal-handshake.db")
    blocker = sqlite3.connect(db_path, isolation_level=None)
    blocker.execute("CREATE TABLE bootstrap_lock (id INTEGER PRIMARY KEY)")
    blocker.execute("BEGIN EXCLUSIVE")
    worker = (
        "import sys\n"
        "from omniagentos.db.migrate import migrate\n"
        "print('ready', flush=True)\n"
        "print(migrate(sys.argv[1]), flush=True)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", worker, db_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        time.sleep(0.1)
        assert process.poll() is None, "migrator crashed instead of waiting on lock"
        blocker.commit()
        stdout, stderr = process.communicate(timeout=30)
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode == 0, stderr
    assert stdout.strip() == str(LATEST_VERSION)


def test_wal_handshake_retry_exhaustion_is_bounded_and_metricized(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = str(tmp_path / "wal-timeout.db")
    blocker = sqlite3.connect(db_path, isolation_level=None)
    blocker.execute("CREATE TABLE bootstrap_lock (id INTEGER PRIMARY KEY)")
    blocker.execute("BEGIN EXCLUSIVE")
    contender = sqlite3.connect(db_path, isolation_level=None)
    caplog.set_level("ERROR", logger="omniagentos.db.store")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _enable_wal(contender, timeout_s=0.0)
    finally:
        contender.close()
        blocker.rollback()
        blocker.close()

    assert "sqlite_wal_init_contention_exhausted retries=1" in caplog.text


def test_legacy_migration_064_schema_converges_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-06: databases that applied original 064 converge through migration 069."""
    legacy_064 = tmp_path / "064_projects_kind.sql"
    legacy_064.write_text(
        """-- Legacy released form of 064.
ALTER TABLE projects ADD COLUMN kind TEXT NOT NULL DEFAULT 'project';
CREATE INDEX IF NOT EXISTS idx_projects_kind_parent
    ON projects(kind, parent_project_id);
UPDATE projects SET kind = 'scratch'
 WHERE name LIKE 'Orchestration:%' OR name LIKE 'Dispatch drill%';
""",
        encoding="utf-8",
    )
    packaged = _migration_files()
    legacy_files = [
        (version, legacy_064 if version == 64 else path)
        for version, path in packaged
        if version <= 67
    ]
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: legacy_files)
    db_path = str(tmp_path / "legacy-064.db")
    assert migrate(db_path) == 67

    with sqlite3.connect(db_path) as connection:
        before = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name LIKE 'idx_projects_kind%'"
            )
        }
    assert before == {"idx_projects_kind_parent"}

    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: packaged)
    assert migrate(db_path) == LATEST_VERSION

    with sqlite3.connect(db_path) as connection:
        after = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name LIKE 'idx_projects_kind%'"
            )
        }
        checksum = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 69"
        ).fetchone()
    assert after == {"idx_projects_kind"}
    assert checksum is not None and checksum[0] == _file_checksum(
        next(path for version, path in packaged if version == 69)
    )
