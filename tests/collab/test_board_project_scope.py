"""Tests for migration 087 + GET /api/board project scoping (P0-7, §3.8/§3.9).

Covers: the 087 column + backfills, dispatch/creation write paths,
server-side ``?project_id=`` filtering, the pre-087 honesty rule (cards with
no run and no chat stay NULL and drop from scoped views), and the
``?parent_task_id=`` children disclosure.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.intake.service import _persist_board_project_id


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def two_projects(store: SqliteStore) -> None:
    now = utc_now_iso()
    for proj_id in ("proj_alpha", "proj_beta"):
        store._connection.execute(
            "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
            (proj_id, f"Project {proj_id}", now),
        )
    store._connection.commit()


def _card(collab_store: CollabStore, title: str, **fields: Any) -> str:
    card = BoardTask(title=title, description=title)
    collab_store.create_board_task(card)
    if fields:
        collab_store.update_board_task(card.id, fields)
    return card.id


class TestMigration087:
    def test_column_and_index_exist(self, store: SqliteStore) -> None:
        cols = store._connection.execute("PRAGMA table_info(board_tasks)").fetchall()
        assert any(col["name"] == "project_id" for col in cols)
        indexes = store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_board_tasks_project'"
        ).fetchall()
        assert len(indexes) == 1

    def test_backfill_sql(self, store: SqliteStore, collab_store: CollabStore) -> None:
        """The two backfills: runner-lane via run chain, companions via chats."""
        now = utc_now_iso()
        conn = store._connection
        conn.execute(
            "INSERT INTO projects (id, name, created_at) VALUES ('proj_bf', 'BF', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO tasks (id, title, state, created_at, updated_at, project_id) "
            "VALUES ('tsk_bf', 'BF task', 'queued', ?, ?, 'proj_bf')",
            (now, now),
        )
        conn.execute(
            "INSERT INTO runs (id, task_id, harness, trace_id, state, queued_at, "
            "created_at, updated_at) VALUES ('run_bf', 'tsk_bf', 'agent', 'tr_bf', "
            "'queued', ?, ?, ?)",
            (now, now, now),
        )
        # runner-lane card (run chain) + a standalone card that must STAY null
        runner_card = _card(collab_store, "runner card", run_id="run_bf")
        standalone = _card(collab_store, "standalone card")
        # a chat companion (chat row links it; origin='chat')
        from omniagentos.chats.store import ChatStore

        chat = ChatStore(store).create_chat(title="BF chat", project_id="proj_bf")
        companion = chat["board_task_id"]
        # undo the creation-time write path so the backfill is what populates it
        conn.execute("UPDATE board_tasks SET project_id = NULL")
        conn.commit()

        # Run the migration's two UPDATE statements verbatim from the file.
        sql = (
            Path("omniagentos/db/migrations/087_board_project_scope.sql").read_text()
        )
        updates = re.findall(r"UPDATE board_tasks .*?;", sql, re.S)
        assert len(updates) == 2
        for statement in updates:
            conn.execute(statement)
        conn.commit()

        rows = {
            row["id"]: row["project_id"]
            for row in conn.execute(
                "SELECT id, project_id FROM board_tasks"
            ).fetchall()
        }
        assert rows[runner_card] == "proj_bf"
        assert rows[companion] == "proj_bf"
        # Honesty: a card with no run and no chat stays NULL.
        assert rows[standalone] is None


class TestWritePaths:
    def test_persist_helper_writes_and_tolerates_missing_column(
        self, collab_store: CollabStore, store: SqliteStore
    ) -> None:
        store._connection.execute(
            "INSERT INTO projects (id, name, created_at) VALUES ('proj_x', 'X', ?)",
            (utc_now_iso(),),
        )
        store._connection.commit()
        card_id = _card(collab_store, "scoped card")
        _persist_board_project_id(collab_store, card_id, "proj_x")
        row = collab_store.get_board_task(card_id)
        assert row["project_id"] == "proj_x"
        # None is a no-op (never clears)
        _persist_board_project_id(collab_store, card_id, None)
        assert collab_store.get_board_task(card_id)["project_id"] == "proj_x"
        # An unknown project never breaks dispatch: best-effort no-op.
        _persist_board_project_id(collab_store, card_id, "proj_deleted")
        assert collab_store.get_board_task(card_id)["project_id"] == "proj_x"

    def test_chat_companion_inherits_project(
        self, store: SqliteStore, collab_store: CollabStore, two_projects: None
    ) -> None:
        from omniagentos.chats.store import ChatStore

        chat = ChatStore(store).create_chat(title="Scoped chat", project_id="proj_alpha")
        row = collab_store.get_board_task(chat["board_task_id"])
        assert row["project_id"] == "proj_alpha"

    def test_spawn_inherits_project(
        self, store: SqliteStore, collab_store: CollabStore, two_projects: None
    ) -> None:
        from omniagentos.chats.store import ChatStore

        chat_store = ChatStore(store)
        chat = chat_store.create_chat(title="Spawn parent", project_id="proj_beta")
        spawn_id = chat_store.create_spawn_task(chat["id"], "child", "child work")
        row = collab_store.get_board_task(spawn_id)
        assert row["project_id"] == "proj_beta"


class TestBoardRoute:
    def test_project_filter_server_side(
        self,
        asgi_client: httpx.AsyncClient,
        collab_store: CollabStore,
        two_projects: None,
    ) -> None:
        alpha = _card(collab_store, "alpha card", project_id="proj_alpha")
        _card(collab_store, "beta card", project_id="proj_beta")
        unscoped = _card(collab_store, "legacy card")

        all_cards = _run(asgi_client.get("/api/board")).json()
        all_ids = {t["id"] for t in all_cards}
        assert {alpha, unscoped} <= all_ids
        # every card emits project_id
        for task in all_cards:
            assert "project_id" in task

        scoped = _run(asgi_client.get("/api/board?project_id=proj_alpha")).json()
        scoped_ids = {t["id"] for t in scoped}
        # Today (pre-fix) this was EMPTY; now exactly the alpha card qualifies.
        assert scoped_ids == {alpha}
        assert scoped[0]["project_id"] == "proj_alpha"

    def test_parent_task_id_returns_children_including_companions(
        self,
        asgi_client: httpx.AsyncClient,
        store: SqliteStore,
        collab_store: CollabStore,
    ) -> None:
        from omniagentos.chats.store import ChatStore

        chat_store = ChatStore(store)
        chat = chat_store.create_chat(title="Parent chat")
        companion = chat["board_task_id"]
        spawn_id = chat_store.create_spawn_task(chat["id"], "child", "work")

        children = _run(
            asgi_client.get(f"/api/board?parent_task_id={companion}")
        ).json()
        child_ids = {t["id"] for t in children}
        assert spawn_id in child_ids
        # The default board hides the companion but shows the spawn (P0-9).
        board = _run(asgi_client.get("/api/board")).json()
        board_ids = {t["id"] for t in board}
        assert companion not in board_ids
        assert spawn_id in board_ids

    def test_chat_origin_field(
        self,
        asgi_client: httpx.AsyncClient,
        store: SqliteStore,
        collab_store: CollabStore,
    ) -> None:
        from omniagentos.chats.store import ChatStore

        chat_store = ChatStore(store)
        chat = chat_store.create_chat(title="Origin chat")
        spawn_id = chat_store.create_spawn_task(chat["id"], "child", "work")
        board = _run(asgi_client.get("/api/board")).json()
        spawn = next(t for t in board if t["id"] == spawn_id)
        # spawned cards have no chat_origin (they are not companions) but DO
        # carry the field
        assert spawn["chat_origin"] is None
        assert "planner_brief" in spawn
