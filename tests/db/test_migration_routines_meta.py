"""Migration F (LOOPS-1): routines.scope + routines.purpose + name-keyed backfill.

Staging SQL remains under migrations-staging/ for far-side apply tests.
Packaged numbered file (when allocated) is included by migrate(); pre-F
shape is obtained by excluding *routines_meta* from _migration_files.
Tests never hard-code a version number.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _iter_sql_statements, _migration_files, migrate

_STAGING = Path(__file__).resolve().parents[2] / "migrations-staging" / "routines_meta.sql"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _apply_staging(connection: sqlite3.Connection) -> None:
    script = _STAGING.read_text(encoding="utf-8")
    for statement in _iter_sql_statements(script):
        connection.execute(statement)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _seed_named_routines(connection: sqlite3.Connection, now: str) -> None:
    names = [
        "improve-lane-dispatcher",
        "lab-jobs-drain",
        "memlife-dream-cycle",
        "goal-review",
        "legacy-untagged",
    ]
    for i, name in enumerate(names):
        connection.execute(
            """
            INSERT INTO routines (
                id, name, description, trigger_type, trigger_config_json,
                task_template_json, gate_type, gate_config_json, hard_cap_type,
                hard_cap_value, notification_target_json, status, auto_pause_reason,
                total_runs, accepted_runs, acceptance_rate, total_cost_usd,
                cost_per_accepted_change, created_at, updated_at, last_fired
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"rtn_seed_{i}",
                name,
                "",
                "cron",
                '{"cron":"0 * * * *"}',
                "{}",
                "exit_code",
                '{"command":"git diff --check"}',
                "max_iterations",
                3.0,
                "{}",
                "active",
                "",
                0,
                0,
                None,
                0.0,
                None,
                now,
                now,
                None,
            ),
        )


def test_staging_sql_exists_and_is_unnumbered() -> None:
    assert _STAGING.is_file(), "migrations-staging/routines_meta.sql must exist"
    # Must NOT already be a numbered migration under omniagentos/db/migrations.
    packaged_names = {p.name for _, p in _migration_files()}
    assert "routines_meta.sql" not in packaged_names
    # Filename itself has no leading NNN_ version (unnumbered).
    assert not _STAGING.name[:3].isdigit()


#: Migrations that REBUILD the `routines` table and therefore assume Migration
#: F's `scope`/`purpose` columns already exist (SQLite's drop-NOT-NULL idiom
#: needs the full current column set, unlike every additive `ALTER TABLE ADD
#: COLUMN` migration between F and here, which does not care what other
#: columns exist). `routines_total_cost_nullable` (migration 119, ISSUE-8) is
#: the first such rebuild since Migration F landed — 091's rebuild predates F
#: and never needed to know about it. Excluded from the "pre-F" simulation
#: below for the same reason 091 itself would have to be if F predated it: a
#: table rebuild is not order-independent of the columns it copies, so this
#: helper's "F is simply missing from the numbered set" fiction cannot also
#: carry a rebuild that hard-requires F's columns. `_apply_migration_119`
#: reintroduces it in its real numbered position (immediately after the
#: staging SQL stands in for F) so the byte-dump comparison this file exists
#: to make still holds. A future migration that rebuilds `routines` again
#: needs the same two-line treatment here.
_REBUILDS_REQUIRING_F = ("routines_meta", "routines_total_cost_nullable")

_MIGRATION_119 = (
    _REPO_ROOT / "omniagentos" / "db" / "migrations" / "119_routines_total_cost_nullable.sql"
)


def _migrations_without_f() -> list[tuple[int, Path]]:
    """Packaged migrations excluding Migration F (routines_meta) for pre-state tests.

    Also excludes any later migration that rebuilds `routines` assuming F's
    columns exist — see `_REBUILDS_REQUIRING_F`.
    """
    return [
        (v, path)
        for v, path in _migration_files()
        if not any(name in path.name for name in _REBUILDS_REQUIRING_F)
    ]


def _apply_migration_119(connection: sqlite3.Connection) -> None:
    """Apply 119 (routines_total_cost_nullable) directly, standing in for its
    real numbered position immediately after Migration F on the "upgraded"
    path — see `_REBUILDS_REQUIRING_F`."""
    script = _MIGRATION_119.read_text(encoding="utf-8")
    for statement in _iter_sql_statements(script):
        connection.execute(statement)


def test_migration_f_adds_scope_purpose_and_backfills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Far side: columns present; system seeders tagged; legacy NULL; goal-review classified."""
    db_path = str(tmp_path / "meta.db")
    # Pre-F shape: all packaged migrations except routines_meta (number may be 093+).
    monkeypatch.setattr(
        "omniagentos.db.migrate._migration_files",
        _migrations_without_f,
    )
    assert migrate(db_path) >= 1

    connection = _connect(db_path)
    try:
        before = _columns(connection, "routines")
        assert "scope" not in before
        assert "purpose" not in before

        now = "2026-07-30T00:00:00Z"
        _seed_named_routines(connection, now)
        _apply_staging(connection)

        after = _columns(connection, "routines")
        assert "scope" in after
        assert "purpose" in after

        rows = {
            row["name"]: row
            for row in connection.execute("SELECT name, scope, purpose FROM routines").fetchall()
        }
        for system_name in (
            "improve-lane-dispatcher",
            "lab-jobs-drain",
            "memlife-dream-cycle",
        ):
            assert rows[system_name]["scope"] == "system"
            assert rows[system_name]["purpose"] is None

        assert rows["goal-review"]["scope"] == "company"
        assert rows["goal-review"]["purpose"] == "goal_review"

        assert rows["legacy-untagged"]["scope"] is None
        assert rows["legacy-untagged"]["purpose"] is None

        # App-side validation (no CHECK): bogus scope is a column write, not a constraint.
        connection.execute(
            "UPDATE routines SET scope = ? WHERE name = ?",
            ("bogus", "legacy-untagged"),
        )
        stored = connection.execute(
            "SELECT scope FROM routines WHERE name = ?",
            ("legacy-untagged",),
        ).fetchone()
        assert stored["scope"] == "bogus"
    finally:
        connection.close()


def _schema_dump(connection: sqlite3.Connection) -> bytes:
    """Canonical byte-comparable dump of the routines table shape.

    Column order is deliberately normalized: later append-only migrations may
    add a column after the staged migration on one upgrade path and before the
    staging SQL in this synthetic comparison, while yielding the same named
    schema. Index/trigger SQL remains part of the fingerprint.
    """
    lines: list[str] = []
    columns = connection.execute("PRAGMA table_info(routines)").fetchall()
    for row in sorted(columns, key=lambda item: str(item[1])):
        # name|type|notnull|dflt_value|pk (cid/order is not semantic)
        lines.append(f"COL|{row[1]}|{row[2]}|{row[3]}|{row[4]}|{row[5]}")
    for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE tbl_name = 'routines' AND type != 'table' "
        "ORDER BY type, name"
    ).fetchall():
        lines.append(f"M|{row[0]}|{row[1]}|{row[2]}|{row[3] or ''}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_fresh_db_with_staging_matches_upgraded_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOOPS1-E1: fresh vs upgraded built by DIFFERENT paths; dumps equal.

    * upgraded: packaged migrate only (existing DB) → then staging ALTER SQL
    * fresh: full migrate that *includes* staged SQL as a virtual next
      migration on a new empty DB (no separate post-apply)
    """
    # --- UPGRADED path: pre-F packaged migrations + staged SQL after ---
    upgraded = str(tmp_path / "upgraded.db")
    monkeypatch.setattr(
        "omniagentos.db.migrate._migration_files",
        _migrations_without_f,
    )
    migrate(upgraded)
    conn_u = _connect(upgraded)
    try:
        # Prove pre-F shape has no scope/purpose, then upgrade in place.
        assert "scope" not in _columns(conn_u, "routines")
        now = "2026-07-30T00:00:00Z"
        _seed_named_routines(conn_u, now)
        _apply_staging(conn_u)
        # 119 (routines_total_cost_nullable) was excluded from the pre-F
        # packaged set for the same reason F was — see _REBUILDS_REQUIRING_F.
        # Reintroduce it here, in its real numbered position immediately
        # after F, so this path reaches the same eventual schema as "fresh".
        _apply_migration_119(conn_u)
        dump_u = _schema_dump(conn_u)
        cols_u = _columns(conn_u, "routines")
    finally:
        conn_u.close()

    # --- FRESH path: virgin DB with full packaged set (includes numbered F) ---
    fresh = str(tmp_path / "fresh.db")
    # Restore real packaged files (incl. numbered Migration F when present).
    monkeypatch.setattr(
        "omniagentos.db.migrate._migration_files",
        _migration_files,
    )
    # Single migrate call — F is part of the fresh-install path when numbered.
    migrate(fresh)
    conn_f = _connect(fresh)
    try:
        dump_f = _schema_dump(conn_f)
        cols_f = _columns(conn_f, "routines")
    finally:
        conn_f.close()

    assert "scope" in cols_u and "purpose" in cols_u
    assert cols_u == cols_f
    # Byte-wise schema dump equality is the binding far side.
    assert dump_u == dump_f, (
        f"fresh vs upgraded schema dumps differ:\n"
        f"upgraded ({len(dump_u)} B) vs fresh ({len(dump_f)} B)"
    )
