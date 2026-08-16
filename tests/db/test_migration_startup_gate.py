"""P1.5: a duplicate migration file must refuse STARTUP, not 500 every request.

Live incident 2026-08-04. ``_migration_files()`` raises when two files claim the
same ``NNN`` prefix, and ``migrate_connection()`` is called from REQUEST paths --
every DAL handed a connection re-migrates it on construction. One stray file
therefore turned a startup-class fault into a 500 on every endpoint, with no
single loud failure anywhere.

The fix is verify-once-then-memoize: the API asserts the inventory at boot (fail
closed, with a message that says what to do), and after that verification the
request paths reuse the verified inventory instead of re-scanning the directory.

Nothing here writes into the real, append-only migrations directory: the
duplicate is built in ``tmp_path`` and the scan is pointed at it.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from omniagentos.api import main as api_main
from omniagentos.db import migrate as migrate_module
from omniagentos.db.migrate import (
    DuplicateMigrationVersion,
    _scan_migration_files,
    migrate_connection,
    verify_migration_files,
)


@pytest.fixture(autouse=True)
def _restore_verified_inventory() -> Iterator[None]:
    """The memo is process-global; never leak one test's state into the next."""
    saved = migrate_module._VERIFIED_MIGRATION_FILES
    try:
        yield
    finally:
        migrate_module._VERIFIED_MIGRATION_FILES = saved


def _duplicate_dir(tmp_path: Path) -> Path:
    """A migrations directory with two files claiming version 001."""
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "001_init.sql").write_text("CREATE TABLE a (id TEXT);", encoding="utf-8")
    (directory / "001_init_copy.sql").write_text("CREATE TABLE b (id TEXT);", encoding="utf-8")
    return directory


def test_scan_rejects_duplicate_versions(tmp_path: Path) -> None:
    with pytest.raises(DuplicateMigrationVersion) as excinfo:
        _scan_migration_files(_duplicate_dir(tmp_path))
    message = str(excinfo.value)
    assert "duplicate migration version" in message
    # The message must name the offenders — the operator has to delete one.
    assert "001_init.sql" in message
    assert "001_init_copy.sql" in message


def test_duplicate_version_is_still_a_runtime_error(tmp_path: Path) -> None:
    """Historical contract: callers catching RuntimeError keep working."""
    with pytest.raises(RuntimeError, match="duplicate migration version"):
        _scan_migration_files(_duplicate_dir(tmp_path))


def test_packaged_directory_verifies(tmp_path: Path) -> None:
    verified = verify_migration_files(refresh=True)
    assert verified, "the packaged migration inventory is empty"
    versions = [version for version, _ in verified]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))


def test_api_startup_refuses_on_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        migrate_module,
        "_scan_migration_files",
        lambda: _scan_migration_files(_duplicate_dir(tmp_path)),
    )
    migrate_module._VERIFIED_MIGRATION_FILES = None

    with pytest.raises(RuntimeError) as excinfo:
        api_main._assert_migration_inventory()

    message = str(excinfo.value)
    assert "REFUSING TO START" in message
    assert "duplicate migration version" in message
    assert isinstance(excinfo.value.__cause__, DuplicateMigrationVersion)


def test_api_startup_passes_on_a_clean_directory() -> None:
    api_main._assert_migration_inventory()  # must not raise
    assert migrate_module._VERIFIED_MIGRATION_FILES is not None


def test_request_paths_are_immune_after_a_clean_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core of the fix: a stray file appearing later cannot 500 requests.

    Startup verified a good inventory; the directory then goes ambiguous. A
    request-path ``migrate_connection`` (what every DAL does with the connection
    it is handed) must keep working off the verified inventory rather than
    re-scanning and re-raising a boot-time fault per request.
    """
    api_main._assert_migration_inventory()  # clean startup

    def now_broken() -> list[tuple[int, Path]]:
        raise DuplicateMigrationVersion("duplicate migration version 042")

    monkeypatch.setattr(migrate_module, "_scan_migration_files", now_broken)

    connection = sqlite3.connect(str(tmp_path / "request.db"), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        version = migrate_connection(connection)
        assert version > 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'notifications'"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_a_failed_refresh_keeps_the_verified_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad re-scan must not poison a process that already verified a good one."""
    good = verify_migration_files(refresh=True)

    def now_broken() -> list[tuple[int, Path]]:
        raise DuplicateMigrationVersion("duplicate migration version 042")

    monkeypatch.setattr(migrate_module, "_scan_migration_files", now_broken)

    with pytest.raises(DuplicateMigrationVersion):
        verify_migration_files(refresh=True)
    assert verify_migration_files() == good


def test_verification_scans_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = _scan_migration_files

    def counted() -> list[tuple[int, Path]]:
        calls.append(1)
        return real()

    monkeypatch.setattr(migrate_module, "_scan_migration_files", counted)
    migrate_module._VERIFIED_MIGRATION_FILES = None

    first = verify_migration_files()
    second = verify_migration_files()

    assert first == second
    assert len(calls) == 1, "the inventory is re-scanned on every call — memo not in effect"


def test_lifespan_refuses_boot_on_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the REAL lifespan, not the helper: an ambiguous directory refuses BOOT.

    ``test_api_startup_refuses_on_duplicate`` proves the guard function raises;
    this proves the guard is actually wired where uvicorn enters — the lifespan
    must re-raise before it ever yields, so nothing downstream of the guard
    (coherence assert, token mint, resume daemons) runs and no request is served.
    """
    monkeypatch.setattr(
        migrate_module,
        "_scan_migration_files",
        lambda: _scan_migration_files(_duplicate_dir(tmp_path)),
    )
    migrate_module._VERIFIED_MIGRATION_FILES = None
    served: list[str] = []

    async def drive() -> None:
        async with api_main.lifespan(api_main.app):
            served.append("yielded")

    with pytest.raises(RuntimeError, match="REFUSING TO START"):
        asyncio.run(drive())
    assert not served, "lifespan yielded (would have served requests) despite an ambiguous inventory"
