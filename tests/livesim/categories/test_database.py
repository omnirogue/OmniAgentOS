"""LiveSim: live runtime DB — migrations, schema, integrity.

Subsystem under observation:

  * the live runtime DB (`var/runtime/state.sqlite3`, WAL, migration
    head 118) read strictly read-only;
  * `omniagentos/db/migrate.py` — append-only `NNN_*.sql` migrations with
    duplicate-version refusal (STARTUP GATE, incident 2026-08-04) and
    checksum fail-closed on edited-after-apply files (M-06);
  * `omniagentos/db/store.py::_connect` — the per-connection pragma contract
    (busy_timeout, WAL, foreign_keys, synchronous).

Safety: the live DB is only ever opened `mode=ro`. Anything destructive —
running migrations, tampering with checksum rows, integrity pragmas — runs
against a scratch copy (sqlite backup API) or a freshly-migrated scratch DB
under `scratch_dir`, and every scratch file is deleted by the test itself.

Numbering note (not a defect): the packaged migration files intentionally
have holes (017-019, 024-029, 056, 075 were never shipped). "No gaps" means
every packaged file at or below the DB head is applied — which these tests
assert — not that the integers are contiguous.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.livesim

REPO = Path(__file__).resolve().parents[3]

# Key live tables and a REQUIRED SUBSET of their columns (subset, not exact
# match, so an append-only column addition does not break the suite).
KEY_TABLE_COLUMNS: dict[str, set[str]] = {
    "schema_migrations": {"version", "applied_at", "checksum"},
    "sessions": {"id", "state", "pid", "provider", "killed_by", "cost_usd",
                 "created_at", "updated_at", "last_activity_at"},
    "swarm_runs": {"id", "board_task_id", "status", "heartbeat_at", "cost_usd"},
    "swarm_attempts": {"id", "swarm_run_id", "session_id", "provider", "model",
                       "end_reason", "cost_usd"},
    "routines": {"id", "name", "trigger_type", "gate_type", "status", "last_fired"},
    "routine_runs": {"id", "routine_id", "gate_passed", "accepted", "cost_usd"},
    "approvals": {"id", "state", "session_id", "action_class", "risk",
                  "expires_at", "created_at"},
    "board_tasks": {"id", "title", "status", "claimed_by", "claim_version", "lane"},
    "tasks": {"id", "title", "state", "risk", "project_id"},
    "provider_call_usage": {"id", "provider", "requested_model", "input_tokens",
                            "output_tokens", "cost_usd_nanos", "cost_quality",
                            "request_state"},
    "session_spawn_queue": {"id", "session_id", "project_dir", "state", "prompt"},
    "skills": {"id", "slug", "status", "current_version"},
    "skill_versions": {"id", "skill_id", "version", "status", "content_digest"},
}


def _migrate_module():
    try:
        from omniagentos.db import migrate as migrate_mod  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot import omniagentos.db.migrate: {exc}")
    return migrate_mod


def _store_connect():
    try:
        from omniagentos.db.store import _connect  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot import omniagentos.db.store: {exc}")
    return _connect


def _packaged_versions(migrate_mod) -> list[tuple[int, Path]]:
    return migrate_mod._scan_migration_files()  # packaged dir; validates uniqueness


def _rm(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()


# ---------------------------------------------------------------------------
# Live DB: migration head, applied-set integrity, checksum ground truth
# ---------------------------------------------------------------------------


@pytest.mark.positive
def test_migration_head_matches_packaged_files_no_gaps(livesim, live_db_ro):
    """The live head equals the newest packaged migration, the applied set has
    no duplicates, and every packaged file at-or-below head is applied (no
    gaps relative to the append-only inventory)."""
    livesim.target("db")
    mm = _migrate_module()
    file_versions = sorted(v for v, _ in _packaged_versions(mm))

    rows = live_db_ro.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    applied = [int(r["version"]) for r in rows]
    head = max(applied)

    livesim.record(
        inputs={"packaged_count": len(file_versions), "packaged_max": max(file_versions)},
        outputs={"head": head, "applied_count": len(applied)},
    )
    livesim.extra(head=head, applied_count=len(applied))

    # no duplicate applied versions
    assert len(applied) == len(set(applied)), "duplicate rows in schema_migrations"
    # every packaged file <= head is applied — a gap here means a skipped migration
    missing = [v for v in file_versions if v <= head and v not in set(applied)]
    assert missing == [], f"packaged migrations at/below head never applied: {missing}"
    # nothing applied that has no packaged file (append-only contract)
    orphans = sorted(set(applied) - set(file_versions))
    assert orphans == [], f"applied versions with no packaged file: {orphans}"
    # the live head is the newest packaged migration (worktree and live checkout in sync)
    assert head == max(file_versions)
    assert head >= 118  # verified live fact 2026-08-06; AGENTS.md says next is 119


@pytest.mark.positive
@pytest.mark.security
def test_applied_checksums_recorded_and_match_disk(livesim, live_db_ro):
    """M-06 ground truth: every applied migration has a recorded sha256 that
    matches the packaged file's bytes — the edited-after-apply tamper defense
    is actually armed, not recorded as NULL."""
    livesim.target("db")
    mm = _migrate_module()
    disk = {v: hashlib.sha256(p.read_bytes()).hexdigest() for v, p in _packaged_versions(mm)}

    rows = live_db_ro.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    null_checksums = [int(r["version"]) for r in rows if r["checksum"] is None]
    mismatches = [
        int(r["version"])
        for r in rows
        if r["checksum"] is not None
        and int(r["version"]) in disk
        and str(r["checksum"]) != disk[int(r["version"])]
    ]
    livesim.record(
        inputs={"applied_rows": len(rows)},
        outputs={"null_checksums": null_checksums, "mismatches": mismatches},
    )
    assert null_checksums == [], f"unbackfilled checksums: {null_checksums}"
    assert mismatches == [], f"checksum drift vs packaged files: {mismatches}"


# ---------------------------------------------------------------------------
# Live DB: journal mode boundary + the ro fixture's write refusal
# ---------------------------------------------------------------------------


@pytest.mark.boundary
@pytest.mark.permission
def test_live_db_wal_mode_and_ro_connection_cannot_write(livesim, live_db_ro):
    """The live DB runs WAL (the store contract), and the livesim ro fixture
    physically refuses a write — the suite's own safety rail is real."""
    livesim.target("db")
    journal = live_db_ro.execute("PRAGMA journal_mode").fetchone()[0]
    busy = live_db_ro.execute("PRAGMA busy_timeout").fetchone()[0]

    write_error = None
    try:
        live_db_ro.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (999999, 'never')"
        )
    except sqlite3.OperationalError as exc:
        write_error = str(exc)

    livesim.record(
        inputs={"attempted": "INSERT INTO schema_migrations (version 999999)"},
        outputs={"journal_mode": journal, "busy_timeout": busy, "write_error": write_error},
    )
    assert journal == "wal"
    assert busy == 10000  # set by the fixture; proves the pragma took effect
    assert write_error is not None and "readonly" in write_error.lower()
    livesim.cleanup(True)  # nothing was (or could be) written


@pytest.mark.boundary
def test_store_connect_pragma_contract_on_scratch_db(livesim, scratch_dir):
    """store._connect() must hand out WAL + busy_timeout=5000 + foreign_keys=ON
    + synchronous=NORMAL + autocommit (isolation_level None) — the contract
    every DAL connection relies on. Exercised on a scratch DB, never live."""
    livesim.target("db", "fs")
    _connect = _store_connect()
    db = scratch_dir / "pragma_probe.sqlite3"
    conn = _connect(str(db))
    try:
        got = {
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "busy_timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
            "foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0],
            "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
            "isolation_level": conn.isolation_level,
        }
    finally:
        conn.close()
    livesim.record(inputs={"db": db.name}, outputs=got)
    assert got["journal_mode"] == "wal"
    assert got["busy_timeout"] == 5000
    assert got["foreign_keys"] == 1
    assert got["synchronous"] == 1  # NORMAL
    assert got["isolation_level"] is None  # autocommit: migrations manage txns manually
    _rm(db)
    livesim.cleanup(not db.exists())


# ---------------------------------------------------------------------------
# Integrity pragmas on a SCRATCH COPY of the live DB (never the live file)
# ---------------------------------------------------------------------------


@pytest.mark.positive
def test_live_db_copy_passes_fk_and_quick_check(livesim, live_db_ro, scratch_dir):
    """A backup-API copy of the live DB has zero foreign_key_check violations
    and passes quick_check — referential + page-level integrity of prod data."""
    livesim.target("db", "fs")
    copy = scratch_dir / "live_copy.sqlite3"
    dst = sqlite3.connect(str(copy))
    try:
        live_db_ro.backup(dst)  # consistent snapshot via the sqlite backup API
        fk_rows = dst.execute("PRAGMA foreign_key_check").fetchall()
        quick = [r[0] for r in dst.execute("PRAGMA quick_check(10)").fetchall()]
    finally:
        dst.close()
    size_mb = round(copy.stat().st_size / 1e6, 1)
    out = {"fk_violations": len(fk_rows), "quick_check": quick, "copy_size_mb": size_mb}
    livesim.evidence("integrity-check.json", json.dumps(out, indent=2))
    livesim.record(inputs={"method": "sqlite backup API from mode=ro conn"}, outputs=out)
    _rm(copy)
    livesim.cleanup(not copy.exists())
    assert fk_rows == [], f"foreign key violations in live data: {fk_rows[:5]}"
    assert quick == ["ok"], f"quick_check reported corruption: {quick[:5]}"


@pytest.mark.positive
def test_key_tables_exist_with_expected_columns(livesim, live_db_ro):
    """Every key runtime table exists and carries its required column subset
    (subset match, so append-only column additions never break this)."""
    livesim.target("db")
    missing_tables: list[str] = []
    missing_columns: dict[str, list[str]] = {}
    for table, required in KEY_TABLE_COLUMNS.items():
        rows = live_db_ro.execute(f"PRAGMA table_info('{table}')").fetchall()
        if not rows:
            missing_tables.append(table)
            continue
        have = {str(r["name"]) for r in rows}
        gap = sorted(required - have)
        if gap:
            missing_columns[table] = gap
    livesim.record(
        inputs={"tables_checked": sorted(KEY_TABLE_COLUMNS)},
        outputs={"missing_tables": missing_tables, "missing_columns": missing_columns},
    )
    assert missing_tables == [], f"key tables absent: {missing_tables}"
    assert missing_columns == {}, f"key columns absent: {missing_columns}"


# ---------------------------------------------------------------------------
# Negative paths: duplicate version refusal + checksum fail-closed (scratch)
# ---------------------------------------------------------------------------


@pytest.mark.negative
def test_duplicate_migration_version_rejected(livesim, scratch_dir, livesim_ns):
    """Two files claiming the same NNN prefix must raise
    DuplicateMigrationVersion (the 2026-08-04 startup-gate incident class).
    Exercised on a scratch directory — the packaged dir is never touched."""
    livesim.target("fs")
    mm = _migrate_module()
    dup_dir = scratch_dir / f"{livesim_ns}_dupmigrations"
    dup_dir.mkdir()
    (dup_dir / "001_first.sql").write_text("CREATE TABLE a (id INTEGER);\n")
    (dup_dir / "001_second.sql").write_text("CREATE TABLE b (id INTEGER);\n")

    with pytest.raises(mm.DuplicateMigrationVersion) as exc_info:
        mm._scan_migration_files(dup_dir)
    msg = str(exc_info.value)

    # and it is still the historical RuntimeError contract
    assert isinstance(exc_info.value, RuntimeError)
    assert "duplicate migration version" in msg and "001" in msg

    # a clean directory scans fine (same code path, no refusal)
    (dup_dir / "001_second.sql").unlink()
    ok = mm._scan_migration_files(dup_dir)
    livesim.record(
        inputs={"files": ["001_first.sql", "001_second.sql"]},
        outputs={"error": msg, "clean_scan_versions": [v for v, _ in ok]},
    )
    assert [v for v, _ in ok] == [1]
    for f in dup_dir.iterdir():
        f.unlink()
    dup_dir.rmdir()
    livesim.cleanup(not dup_dir.exists())


@pytest.mark.negative
@pytest.mark.security
def test_edited_after_apply_checksum_fails_closed_on_scratch(livesim, scratch_dir):
    """Tampering a recorded checksum on a SCRATCH DB makes the next migrate
    raise MigrationChecksumMismatch instead of proceeding — M-06 fail-closed,
    demonstrated without ever touching the live DB or a packaged file."""
    livesim.target("db", "fs")
    mm = _migrate_module()
    _connect = _store_connect()
    db = scratch_dir / "tamper_probe.sqlite3"
    head = mm.migrate(str(db))
    conn = _connect(str(db))
    try:
        conn.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 1",
            ("0" * 64,),  # plausible-but-wrong sha256
        )
        with pytest.raises(mm.MigrationChecksumMismatch) as exc_info:
            mm.migrate_connection(conn)
        msg = str(exc_info.value)
    finally:
        conn.close()
    livesim.record(
        inputs={"tampered_version": 1, "fake_checksum": "0" * 64},
        outputs={"head_before": head, "error": msg},
    )
    assert "001" in msg and "edited after" in msg
    _rm(db)
    livesim.cleanup(not db.exists())


# ---------------------------------------------------------------------------
# Fresh-DB migrate: reaches head, idempotent on re-run (scratch)
# ---------------------------------------------------------------------------


@pytest.mark.positive
@pytest.mark.recovery
def test_fresh_scratch_db_migrates_to_head_and_is_idempotent(livesim, scratch_dir):
    """migrate() on an empty scratch DB replays the full append-only chain to
    the packaged head; a second migrate is a no-op (same head, same row count)
    — the recovery path a rebuilt runtime DB depends on."""
    livesim.target("db", "fs")
    mm = _migrate_module()
    file_max = max(v for v, _ in _packaged_versions(mm))
    db = scratch_dir / "fresh_probe.sqlite3"

    head1 = mm.migrate(str(db))
    conn = sqlite3.connect(str(db))
    try:
        rows1 = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    finally:
        conn.close()
    head2 = mm.migrate(str(db))
    conn = sqlite3.connect(str(db))
    try:
        rows2 = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
    finally:
        conn.close()
    out = {"head_first": head1, "head_second": head2, "rows_first": rows1,
           "rows_second": rows2, "tables_created": tables}
    livesim.record(inputs={"db": db.name, "packaged_max": file_max}, outputs=out)
    livesim.extra(tables_created=tables)
    assert head1 == file_max  # a fresh DB reaches the packaged head
    assert head2 == head1  # idempotent: re-run changes nothing
    assert rows1 == rows2 == len(_packaged_versions(mm))
    # every key table the live system depends on exists on a fresh migrate
    conn = sqlite3.connect(str(db))
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    missing = sorted(set(KEY_TABLE_COLUMNS) - names)
    assert missing == [], f"fresh migrate did not create key tables: {missing}"
    _rm(db)
    livesim.cleanup(not db.exists())
