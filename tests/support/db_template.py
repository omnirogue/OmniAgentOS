"""Pre-migrated SQLite template databases for tests.

Why this exists
---------------
Every store constructor (``SqliteStore``, ``CollabStore``, ``CsiStore``, …)
runs :func:`omniagentos.db.migrate.migrate_connection`, which applies the 107
packaged migration files. On a fresh file that is ~130-330 ms of pure DDL, and
the fast lane pays it ~6,100 times (measured: ~500 s of cumulative test time,
~31% of the lane). The overwhelming majority of those calls come from
*function-scoped* fixtures that build a throwaway database per test.

The fix is a template: migrate ONCE per process per store class into a private
temp dir, then hand each test a byte-for-byte ``shutil.copyfile`` of it. The
store constructor still runs ``migrate_connection`` on the copy, but that is
now the already-migrated no-op path (a version check plus checksum
re-verification) rather than 107 file applies. Measured on this repo:

    CollabStore fresh construct   327.6 ms  ->  copy + construct  20.0 ms
    CsiStore    fresh construct   363.4 ms  ->  copy + construct  25.4 ms

Correctness properties
----------------------
* **Same schema.** The copy is the same bytes as a freshly migrated database:
  ``sqlite_master`` (514 objects) and ``schema_migrations`` (107 rows) compare
  equal, and every table has the same row count as a fresh build.
* **Same checksum contract.** ``migrate_connection`` still runs on every copy
  and relies on migrate.py's shared memoised, race-safe checksum mechanism, so
  ``MigrationChecksumMismatch`` still protects against edited applied files.
* **Template inventory matches the applied count.** The digest cache key comes
  from ``verify_migration_files(refresh=True)`` while the store constructor
  applies migrations via migrate.py's own ``_migration_files()``; in the
  unpatched case both read the same on-disk directory, so they agree. A caller
  that monkeypatches ``migrate._migration_files`` directly (some tests do, to
  exercise a subset of migrations) can desynchronise the two, so the count of
  applied ``schema_migrations`` rows in the freshly built template is checked
  against the digest's inventory length before the template is cached; a
  mismatch raises instead of silently caching a partial schema under a
  full-inventory key.
* **No cross-test leakage.** The template is written once and thereafter only
  ever read; each test gets its own file copy. Nothing is shared between tests
  beyond immutable bytes on disk.
* **xdist-safe.** The cache and the temp dir are process-local, so every
  ``gw*`` worker builds and owns its own template. No inter-process locking,
  no shared path, nothing to race on.
* **WAL-safe.** ``_connect`` opens databases in WAL mode, so a plain file copy
  would be incomplete if committed frames were still sitting in the ``-wal``
  sidecar. The template is checkpointed with ``PRAGMA wal_checkpoint(TRUNCATE)``
  and then *verified* to have an empty/absent ``-wal`` before it is cached;
  a template that cannot be made copy-safe raises instead of silently handing
  out a truncated database.

Usage
-----
Replace a per-test migrating construction::

    @pytest.fixture
    def db_path(tmp_path):
        db = str(tmp_path / "summary.db")
        CollabStore(db)          # ~330 ms of migrations, every test
        return db

with::

    from tests.support.db_template import migrated_db

    @pytest.fixture
    def db_path(tmp_path):
        return migrated_db(CollabStore, tmp_path / "summary.db")

and a per-test store with :func:`make_store`::

    store = make_store(SqliteStore, tmp_path / "scope.db")

Only file-backed databases are supported; ``":memory:"`` cannot be copied and
is rejected loudly rather than silently falling back.
"""

from __future__ import annotations

import atexit
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path
from typing import Any, TypeVar

from omniagentos.db.migrate import _migration_inventory_digest, verify_migration_files

__all__ = ["make_store", "migrated_db", "template_for"]

_T = TypeVar("_T")

# Process-local: xdist workers are separate processes, so each worker builds
# and owns exactly one template per store class. Never share this across
# processes -- a half-written template is worse than a slow one.
# The checksum-bearing inventory digest is part of the cache key: a migration
# edit/addition in a long-running test process must rebuild its template.
_TEMPLATES: dict[str, tuple[str, Path]] = {}
_TEMPLATE_DIR: Path | None = None
_LOCK = threading.Lock()
_TEMPLATE_REBUILD_ATTEMPTS = 5


def _template_dir() -> Path:
    global _TEMPLATE_DIR
    if _TEMPLATE_DIR is None:
        created = Path(tempfile.mkdtemp(prefix="oaos-db-template-"))
        atexit.register(shutil.rmtree, created, ignore_errors=True)
        _TEMPLATE_DIR = created
    return _TEMPLATE_DIR


def _sidecars(db: Path) -> tuple[Path, Path]:
    return db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")


def _make_copy_safe(db: Path) -> None:
    """Fold the WAL back into the main database file, then prove it worked.

    ``shutil.copyfile`` copies one file. In WAL mode a committed transaction
    may live only in the ``-wal`` sidecar, so copying the main file alone would
    hand out a database missing recent commits. A TRUNCATE checkpoint moves
    every committed frame into the main file and empties the WAL.
    """
    connection = sqlite3.connect(str(db))
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()

    wal, _shm = _sidecars(db)
    if wal.exists() and wal.stat().st_size > 0:
        raise RuntimeError(
            f"template {db.name} still has a non-empty WAL after a TRUNCATE "
            f"checkpoint ({wal.stat().st_size} bytes); a file copy of it would "
            "be incomplete. A connection to the template is still open."
        )


def _close_quietly(obj: object) -> None:
    close = getattr(obj, "close", None)
    if callable(close):
        close()


def _current_inventory() -> tuple[str, list[tuple[int, Path]]]:
    """The verified inventory used by the next template migration, and its digest.

    Both are derived from :func:`verify_migration_files`, never from
    ``migrate._migration_files()`` directly -- a caller may monkeypatch the
    latter to exercise a subset of migrations (some tests do), and this must
    not desynchronise from what actually gets applied. See the applied-count
    post-condition in :func:`template_for` for the case where it does anyway.
    """
    files = verify_migration_files(refresh=True)
    return _migration_inventory_digest(files), files


def template_for(store_cls: type) -> Path:
    """Path to this process's pre-migrated template for ``store_cls``.

    Built on first use (one full migration chain), and rebuilt if the current
    migration-file inventory changes. The returned file is read-only by
    convention: callers copy it, never open it for writing.
    """
    key = f"{store_cls.__module__}.{store_cls.__qualname__}"
    inventory, files = _current_inventory()
    cached = _TEMPLATES.get(key)
    if cached is not None and cached[0] == inventory:
        return cached[1]

    with _LOCK:
        inventory, files = _current_inventory()
        cached = _TEMPLATES.get(key)
        if cached is not None and cached[0] == inventory:
            return cached[1]

        path = _template_dir() / f"{key}.db"
        for _attempt in range(_TEMPLATE_REBUILD_ATTEMPTS):
            # Build into a scratch name first so a failed build can never leave
            # a partially-migrated file behind under the cached path.
            scratch = path.with_name(path.name + ".building")
            for stale in (scratch, *_sidecars(scratch)):
                stale.unlink(missing_ok=True)
            store = store_cls(str(scratch))
            try:
                _close_quietly(store)
                _make_copy_safe(scratch)
            except BaseException:
                for junk in (scratch, *_sidecars(scratch)):
                    junk.unlink(missing_ok=True)
                raise

            rebuilt_inventory, rebuilt_files = _current_inventory()
            if rebuilt_inventory != inventory:
                for junk in (scratch, *_sidecars(scratch)):
                    junk.unlink(missing_ok=True)
                inventory, files = rebuilt_inventory, rebuilt_files
                continue

            # Check the ARTIFACT, not merely the path that produced it: the
            # store constructor applies migrations via migrate.py's own
            # ``_migration_files()``, which some tests monkeypatch directly
            # (bypassing ``verify_migration_files``) to exercise a subset of
            # migrations. That can desynchronise the schema this build
            # actually produced from ``files`` (the inventory the digest
            # above was computed from), and this is immune regardless of how
            # ``files`` was obtained.
            connection = sqlite3.connect(str(scratch))
            try:
                applied = connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations"
                ).fetchone()[0]
            except sqlite3.OperationalError as exc:
                # A store class whose constructor does not run migrations lands
                # here. Say so, rather than surfacing a bare "no such table" on
                # shared infrastructure every ``migrated_db()`` caller depends on.
                raise RuntimeError(
                    f"template for {key} has no schema_migrations table — "
                    f"{store_cls.__name__} did not run migrations"
                ) from exc
            finally:
                connection.close()
            if applied != len(files):
                for junk in (scratch, *_sidecars(scratch)):
                    junk.unlink(missing_ok=True)
                raise RuntimeError(
                    f"template for {key} applied {applied} migrations but the "
                    f"verified inventory has {len(files)}; refusing to cache "
                    "it under a full-inventory digest (likely a monkeypatched "
                    "migrate._migration_files during a template build)"
                )

            scratch.replace(path)
            for junk in _sidecars(scratch):
                junk.unlink(missing_ok=True)
            _TEMPLATES[key] = (inventory, path)
            return path

        raise RuntimeError(
            "migration inventory changed while building template after "
            f"{_TEMPLATE_REBUILD_ATTEMPTS} attempts: {path}"
        )


def migrated_db(store_cls: type, dest: str | Path) -> str:
    """Materialise an already-migrated database at ``dest`` and return its path.

    Equivalent to ``store_cls(str(dest))`` for schema purposes, minus the 107
    migration applies. The caller still constructs whatever store it needs on
    the returned path; that construction re-runs ``migrate_connection``, which
    now finds every version applied and only re-verifies checksums.
    """
    destination = Path(dest)
    if str(destination) == ":memory:":
        raise ValueError(
            "migrated_db() needs a file path; ':memory:' databases cannot be "
            "copied from a template (use the store constructor directly)"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    # A leftover sidecar from an earlier database at this path would be
    # interpreted against the new file. Tests use fresh tmp_path dirs, but be
    # explicit rather than lucky.
    for sidecar in _sidecars(destination):
        sidecar.unlink(missing_ok=True)
    shutil.copyfile(template_for(store_cls), destination)
    return str(destination)


def make_store(store_cls: Any, dest: str | Path, *args: Any, **kwargs: Any) -> Any:
    """``store_cls`` built on a pre-migrated copy of the template at ``dest``."""
    return store_cls(migrated_db(store_cls, dest), *args, **kwargs)
