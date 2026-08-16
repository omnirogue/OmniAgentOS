"""The conversation contract: ONE ordering, and ``folder`` as a first-class field.

Two defects measured on the live product:

* ``GET /api/chats/{id}/messages`` returned turns oldest-first while
  ``GET /api/board/{task_id}/conversation`` returned them newest-first, so any
  component holding "a conversation" had to know which endpoint produced it.
  Both are chronological now; the board route's ``limit`` still selects the most
  RECENT turns, it just presents them in order.
* a chat's ``folder`` — which has a registry table, three routes and a query
  parameter of its own — was reachable only by digging into the free-form
  ``meta`` blob on the wire.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

import omniagentos.api.main  # noqa: F401 -- break the package's documented import cycle.
from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.categories import get_longhaul_store
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.db.migrate import migrate
from omniagentos.longhaul import LonghaulStore
from omniagentos.sessions.token import load_or_create_token


def _run(coro: Any) -> httpx.Response:
    return asyncio.run(coro)


@pytest.fixture
def conversation_api(tmp_path: Path) -> Iterator[tuple[httpx.AsyncClient, CollabStore, str]]:
    db_path = str(tmp_path / "conversation.db")
    migrate(db_path)
    collab = CollabStore(db_path)
    longhaul = LonghaulStore(db_path)
    app.dependency_overrides[get_store] = lambda: collab._store
    app.dependency_overrides[get_collab_store] = lambda: collab
    app.dependency_overrides[get_longhaul_store] = lambda: longhaul
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client, collab, load_or_create_token()
    finally:
        app.dependency_overrides.clear()
        asyncio.run(client.aclose())
        longhaul.close()


def _headers(token: str) -> dict[str, str]:
    return {"X-Session-Token": token}


class TestBoardConversationOrdering:
    def test_turns_come_back_oldest_first(
        self, conversation_api: tuple[httpx.AsyncClient, CollabStore, str]
    ) -> None:
        client, collab, token = conversation_api
        task = BoardTask(id="btk_order", title="ordering probe")
        collab.create_board_task(task)
        for content in ("first", "second", "third"):
            posted = _run(
                client.post(
                    f"/api/board/{task.id}/message",
                    json={"content": content},
                    headers=_headers(token),
                )
            )
            assert posted.status_code == 200, posted.text

        turns = _run(
            client.get(f"/api/board/{task.id}/conversation", headers=_headers(token))
        ).json()
        assert [turn["content"] for turn in turns] == ["first", "second", "third"]
        assert [turn["seq"] for turn in turns] == sorted(turn["seq"] for turn in turns)

    def test_limit_still_selects_the_most_recent_window(
        self, conversation_api: tuple[httpx.AsyncClient, CollabStore, str]
    ) -> None:
        """Chronological presentation must not turn ``limit`` into "the OLDEST n"."""
        client, collab, token = conversation_api
        task = BoardTask(id="btk_window", title="window probe")
        collab.create_board_task(task)
        for content in ("t1", "t2", "t3", "t4"):
            _run(
                client.post(
                    f"/api/board/{task.id}/message",
                    json={"content": content},
                    headers=_headers(token),
                )
            )
        turns = _run(
            client.get(
                f"/api/board/{task.id}/conversation",
                params={"limit": 2},
                headers=_headers(token),
            )
        ).json()
        assert [turn["content"] for turn in turns] == ["t3", "t4"]

    def test_matches_the_chat_messages_ordering(
        self, conversation_api: tuple[httpx.AsyncClient, CollabStore, str]
    ) -> None:
        """The point of the change: both conversation reads agree."""
        client, collab, token = conversation_api
        task = BoardTask(id="btk_parity", title="parity probe")
        collab.create_board_task(task)
        for content in ("alpha", "beta"):
            _run(
                client.post(
                    f"/api/board/{task.id}/message",
                    json={"content": content},
                    headers=_headers(token),
                )
            )
        chat = _run(client.post("/api/chats", json={"title": "parity chat"})).json()
        from omniagentos.conversations.store import ConversationStore

        conversations = ConversationStore(collab._store)
        for content in ("alpha", "beta"):
            conversations.append("chat", chat["id"], "user", content)

        board_turns = _run(
            client.get(f"/api/board/{task.id}/conversation", headers=_headers(token))
        ).json()
        chat_turns = _run(client.get(f"/api/chats/{chat['id']}/messages")).json()
        assert [t["content"] for t in board_turns] == [t["content"] for t in chat_turns]


class TestChatFolderField:
    def test_folder_is_first_class_on_the_list_dto(self, asgi_client: httpx.AsyncClient) -> None:
        created = _run(asgi_client.post("/api/chats", json={"title": "filed chat"})).json()
        _run(asgi_client.patch(f"/api/chats/{created['id']}", json={"folder": "Research"}))

        listed = _run(asgi_client.get("/api/chats")).json()
        row = next(chat for chat in listed if chat["id"] == created["id"])
        assert row["folder"] == "Research"
        # meta stays the storage, so nothing reading it today breaks.
        assert row["meta"]["folder"] == "Research"

    def test_unfiled_chat_reports_null_not_empty_string(
        self, asgi_client: httpx.AsyncClient
    ) -> None:
        created = _run(asgi_client.post("/api/chats", json={"title": "unfiled chat"})).json()
        assert created["folder"] is None
        listed = _run(asgi_client.get("/api/chats")).json()
        assert next(c for c in listed if c["id"] == created["id"])["folder"] is None

    def test_detail_read_carries_the_same_field(self, asgi_client: httpx.AsyncClient) -> None:
        created = _run(asgi_client.post("/api/chats", json={"title": "detail chat"})).json()
        patched = _run(
            asgi_client.patch(f"/api/chats/{created['id']}", json={"folder": "Ops"})
        ).json()
        assert patched["folder"] == "Ops"
        detail = _run(asgi_client.get(f"/api/chats/{created['id']}")).json()
        assert detail["folder"] == "Ops"

    def test_clearing_the_folder_reports_null(self, asgi_client: httpx.AsyncClient) -> None:
        created = _run(asgi_client.post("/api/chats", json={"title": "moving chat"})).json()
        _run(asgi_client.patch(f"/api/chats/{created['id']}", json={"folder": "Ops"}))
        cleared = _run(asgi_client.patch(f"/api/chats/{created['id']}", json={"folder": ""}))
        assert cleared.json()["folder"] is None
