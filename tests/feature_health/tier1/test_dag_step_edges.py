"""dag_step_edges FK semantics — what migration 096 declares must actually hold.

096_dag_moe_gating.sql (renamed from the colliding graph_edges name) declares
``dag_step_edges(parent_step_id/child_step_id) REFERENCES steps(id) ON DELETE
CASCADE``.  This verifies the migrated schema on a tmp DB under the product's
own connection pragmas (``db.store._connect`` sets PRAGMA foreign_keys=ON, the
same setting the DAL uses — SQLite FKs are per-connection and default OFF).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import migrate
from omniagentos.db.store import _connect


def _seed_steps(connection: sqlite3.Connection) -> tuple[int, int, int]:
    """Insert the FK chain the schema requires: task -> run -> three steps."""
    now = "2026-07-31T00:00:00Z"
    connection.execute(
        "INSERT INTO tasks (id, title, created_at, updated_at) VALUES (?,?,?,?)",
        ("tsk_fh_dag", "fh dag edges", now, now),
    )
    connection.execute(
        "INSERT INTO runs (id, task_id, harness, trace_id, queued_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("run_fh_dag", "tsk_fh_dag", "mock", "trace_fh_dag", now, now, now),
    )
    step_ids: list[int] = []
    for seq, name in enumerate(("parent", "child", "bystander"), start=1):
        cursor = connection.execute(
            "INSERT INTO steps (run_id, seq, name, checkpoint_json) VALUES (?,?,?,?)",
            ("run_fh_dag", seq, name, "{}"),
        )
        step_ids.append(int(cursor.lastrowid))
    return step_ids[0], step_ids[1], step_ids[2]


@pytest.fixture()
def migrated_conn(tmp_path: Path):
    db_path = tmp_path / "dag-edges.db"
    migrate(str(db_path))
    connection = _connect(str(db_path))
    try:
        yield connection
    finally:
        connection.close()


def test_schema_declares_cascade_on_both_fks(migrated_conn: sqlite3.Connection) -> None:
    fks = migrated_conn.execute("PRAGMA foreign_key_list(dag_step_edges)").fetchall()
    by_column = {row["from"]: row for row in fks}
    assert set(by_column) == {"parent_step_id", "child_step_id"}
    for column in ("parent_step_id", "child_step_id"):
        assert by_column[column]["table"] == "steps"
        assert by_column[column]["on_delete"] == "CASCADE", (
            f"schema promises ON DELETE CASCADE for {column}"
        )


def test_deleting_parent_step_cascades_edge_rows(
    migrated_conn: sqlite3.Connection,
) -> None:
    parent, child, bystander = _seed_steps(migrated_conn)
    migrated_conn.execute(
        "INSERT INTO dag_step_edges (id, parent_step_id, child_step_id) VALUES (?,?,?)",
        ("dse_victim", parent, child),
    )
    migrated_conn.execute(
        "INSERT INTO dag_step_edges (id, parent_step_id, child_step_id) VALUES (?,?,?)",
        ("dse_survivor", child, bystander),
    )
    assert migrated_conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    migrated_conn.execute("DELETE FROM steps WHERE id = ?", (parent,))

    remaining = {
        row["id"]
        for row in migrated_conn.execute("SELECT id FROM dag_step_edges").fetchall()
    }
    # The edge naming the deleted step (as parent) cascades away; the edge
    # between two surviving steps is untouched.
    assert remaining == {"dse_survivor"}


def test_deleting_child_step_cascades_via_child_fk(
    migrated_conn: sqlite3.Connection,
) -> None:
    parent, child, _ = _seed_steps(migrated_conn)
    migrated_conn.execute(
        "INSERT INTO dag_step_edges (id, parent_step_id, child_step_id) VALUES (?,?,?)",
        ("dse_child_side", parent, child),
    )
    migrated_conn.execute("DELETE FROM steps WHERE id = ?", (child,))
    assert migrated_conn.execute("SELECT COUNT(*) FROM dag_step_edges").fetchone()[0] == 0


def test_dangling_edge_insert_is_refused(migrated_conn: sqlite3.Connection) -> None:
    _seed_steps(migrated_conn)
    with pytest.raises(sqlite3.IntegrityError):
        migrated_conn.execute(
            "INSERT INTO dag_step_edges (id, parent_step_id, child_step_id) VALUES (?,?,?)",
            ("dse_dangling", 999_999, 999_998),
        )
