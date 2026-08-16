"""Tests for :class:`ConversationStore` against the FROZEN conversations schema.

The conversations table + ``projects.parent_project_id`` are owned by W3-hierarchy
(migration 031), which ships for real in this integrated repo — so ``migrate()`` alone
is enough for the happy-path tests below. The two degradation tests still need a
pre-031-shaped database, so they DROP what migration 031 adds after a full migrate();
``ConversationStore`` only ever sees an ``sqlite3.OperationalError`` either way (whether
the table/column never existed or was removed), so this is a faithful stand-in for a
genuinely unmigrated database.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.memory.contracts import ScopeRef
from omniagentos.memory.store import ConversationStore


def _store_with_hierarchy(tmp_path: Path) -> SqliteStore:
    db_path = tmp_path / "memory.db"
    migrate(str(db_path))
    return SqliteStore(str(db_path))


def _make_project(store: SqliteStore, project_id: str, parent: str | None) -> None:
    store._connection.execute(
        "INSERT INTO projects (id, name, created_at, parent_project_id) VALUES (?, ?, ?, ?)",
        (project_id, project_id, utc_now_iso(), parent),
    )


def _make_task(store: SqliteStore, task_id: str, project_id: str | None) -> None:
    now = utc_now_iso()
    store.create_task(
        {
            "id": task_id,
            "discipline_id": None,
            "project_id": project_id,
            "title": "t",
            "input_json": json.dumps({}),
            "acceptance_json": "{}",
            "state": "queued",
            "risk": "low",
            "created_at": now,
            "updated_at": now,
        }
    )


def test_append_allocates_sequential_seq_and_reads_chronologically(tmp_path: Path) -> None:
    store = _store_with_hierarchy(tmp_path)
    conv = ConversationStore(store)

    t0 = conv.append_turn("task", "tsk_1", "user", "first")
    t1 = conv.append_turn("task", "tsk_1", "agent", "second", model="claude")
    t2 = conv.append_turn("task", "tsk_1", "user", "third")
    assert t0 is not None and t0.seq == 0
    assert t1 is not None and t1.seq == 1 and t1.model == "claude"
    assert t2 is not None and t2.seq == 2

    # A different scope has its own independent seq counter.
    other = conv.append_turn("project", "proj_x", "user", "other-first")
    assert other is not None and other.seq == 0

    turns = conv.recent_turns("task", "tsk_1", 10)
    assert [t.content for t in turns] == ["first", "second", "third"]
    assert [t.seq for t in turns] == [0, 1, 2]

    # LIMIT keeps the most recent, still returned ascending.
    recent = conv.recent_turns("task", "tsk_1", 2)
    assert [t.content for t in recent] == ["second", "third"]


def test_resolve_ancestors_walks_project_hierarchy_root_first(tmp_path: Path) -> None:
    store = _store_with_hierarchy(tmp_path)
    _make_project(store, "proj_root", None)
    _make_project(store, "proj_child", "proj_root")
    _make_task(store, "tsk_leaf", "proj_child")
    conv = ConversationStore(store)

    # A task inherits its project chain, root first, its own project last.
    assert conv.resolve_ancestors("task", "tsk_leaf") == [
        ScopeRef("project", "proj_root"),
        ScopeRef("project", "proj_child"),
    ]
    # A project's ancestors exclude itself.
    assert conv.resolve_ancestors("project", "proj_child") == [ScopeRef("project", "proj_root")]
    assert conv.resolve_ancestors("project", "proj_root") == []
    # A task with no project has no ancestors.
    _make_task(store, "tsk_orphan", None)
    assert conv.resolve_ancestors("task", "tsk_orphan") == []


def test_rolling_summary_prefers_marked_summary_then_first_user_turn(tmp_path: Path) -> None:
    store = _store_with_hierarchy(tmp_path)
    conv = ConversationStore(store)
    # With only user/agent turns, the summary falls back to the FIRST user turn.
    conv.append_turn("task", "tsk_1", "user", "the original brief")
    conv.append_turn("task", "tsk_1", "agent", "did it")
    assert conv.rolling_summary("task", "tsk_1") == "the original brief"

    # An explicit system summary turn takes precedence.
    conv.append_turn("task", "tsk_1", "system", "rolling summary v2", meta={"kind": "summary"})
    assert conv.rolling_summary("task", "tsk_1") == "rolling summary v2"

    assert conv.rolling_summary("task", "tsk_absent") is None


def test_degrades_gracefully_without_conversations_table(tmp_path: Path) -> None:
    # Migrate fully, then drop what 031 adds to simulate a pre-031 database: no
    # conversations table, no parent_project_id column.
    db_path = tmp_path / "bare.db"
    migrate(str(db_path))
    store = SqliteStore(str(db_path))
    store._connection.executescript("DROP TABLE conversations;")
    conv = ConversationStore(store)

    assert conv.recent_turns("task", "tsk_1", 10) == []
    assert conv.append_turn("task", "tsk_1", "user", "x") is None
    assert conv.rolling_summary("task", "tsk_1") is None
    _make_task(store, "tsk_1", None)
    assert conv.resolve_ancestors("task", "tsk_1") == []


def test_missing_parent_column_yields_no_ancestors(tmp_path: Path) -> None:
    # conversations table present but parent_project_id absent: ancestor walk stops.
    # Migrate fully (so conversations is real), then drop just the column to simulate
    # that half of a pre-031 database.
    db_path = tmp_path / "partial.db"
    migrate(str(db_path))
    store = SqliteStore(str(db_path))
    store._connection.executescript(
        "DROP INDEX IF EXISTS idx_projects_parent;"
        "DROP INDEX IF EXISTS idx_projects_kind_parent;"
        "DROP INDEX IF EXISTS idx_projects_kind;"
        "ALTER TABLE projects DROP COLUMN parent_project_id;"
    )
    store._connection.execute(
        "INSERT INTO projects (id, name, created_at) VALUES ('proj_p', 'proj_p', ?)",
        (utc_now_iso(),),
    )
    _make_task(store, "tsk_1", "proj_p")
    conv = ConversationStore(store)
    # No parent_project_id column -> _parent_of returns None -> only... nothing, because
    # the include_self chain still needs the column to walk; the immediate project is
    # returned as the leaf of the chain even with no ancestry.
    assert conv.resolve_ancestors("task", "tsk_1") == [ScopeRef("project", "proj_p")]


@pytest.mark.parametrize("scope_type", ["task", "project"])
def test_append_and_read_roundtrip_for_both_scopes(tmp_path: Path, scope_type: str) -> None:
    store = _store_with_hierarchy(tmp_path)
    conv = ConversationStore(store)
    conv.append_turn(scope_type, "id_1", "user", "hello")
    turns = conv.recent_turns(scope_type, "id_1", 10)
    assert len(turns) == 1
    assert turns[0].role == "user"
    assert turns[0].content == "hello"
