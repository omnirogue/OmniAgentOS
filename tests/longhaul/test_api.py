"""API coverage for the W4 longhaul routes and their privacy boundary."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.categories import get_longhaul_store
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.migrate import migrate
from omniagentos.longhaul import LonghaulStore
from omniagentos.sessions.token import load_or_create_token


@pytest.fixture
def api_fixture(tmp_path: Path) -> tuple[httpx.AsyncClient, CollabStore, LonghaulStore, str]:
    db_path = str(tmp_path / "api.db")
    migrate(db_path)
    collab = CollabStore(db_path)
    longhaul = LonghaulStore(db_path)
    app.dependency_overrides[get_store] = lambda: collab._store
    app.dependency_overrides[get_collab_store] = lambda: collab
    app.dependency_overrides[get_longhaul_store] = lambda: longhaul
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client, collab, longhaul, load_or_create_token()
    finally:
        app.dependency_overrides.clear()
        asyncio.run(client.aclose())
        longhaul.close()


def call(coro: Any) -> httpx.Response:
    return asyncio.run(coro)


def headers(token: str) -> dict[str, str]:
    return {"X-Session-Token": token}


def test_categories_crud_and_events(api_fixture: Any) -> None:
    client, _, _, token = api_fixture
    created = call(
        client.post(
            "/api/categories",
            json={"name": "Research", "color": "purple", "wip_limit": 2},
            headers=headers(token),
        )
    )
    assert created.status_code == 201
    category = created.json()
    listed = call(client.get("/api/categories", headers=headers(token)))
    assert listed.status_code == 200
    assert listed.json() == [category]

    updated = call(
        client.patch(
            f"/api/categories/{category['id']}",
            json={"color": "blue"},
            headers=headers(token),
        )
    )
    assert updated.status_code == 200
    assert updated.json()["color"] == "blue"


def test_category_errors_and_get_auth(api_fixture: Any) -> None:
    client, _, _, token = api_fixture
    assert call(client.get("/api/categories")).status_code == 401
    assert (
        call(
            client.patch("/api/categories/missing", json={"name": "x"}, headers=headers(token))
        ).status_code
        == 404
    )


def _task(collab: CollabStore, task_id: str = "btk_api") -> BoardTask:
    task = BoardTask(id=task_id, title="Longhaul API task")
    collab.create_board_task(task)
    return task


def test_message_conversation_and_sse_privacy(api_fixture: Any) -> None:
    client, collab, _, token = api_fixture
    task = _task(collab)
    response = call(
        client.post(
            f"/api/board/{task.id}/message",
            json={"content": "secret steering instruction"},
            headers=headers(token),
        )
    )
    assert response.status_code == 200
    turn = response.json()
    assert turn["content"] == "secret steering instruction"
    assert turn["seq"] == 0

    conversation = call(
        client.get(f"/api/board/{task.id}/conversation?limit=50", headers=headers(token))
    )
    assert conversation.status_code == 200
    # New steering turns start undelivered; the pending marker is what the
    # engine's finish-refusal check and prompt re-injection key off.
    assert conversation.json()[0]["delivery_status"] == {"pending": True}

    events = collab._store.get_events_after(0)
    event = next(event for event in events if event["type"] == "task.message")
    payload = json.loads(event["payload_json"])
    assert payload == {"task_id": task.id, "turn_id": turn["id"], "seq": 0}
    assert "secret steering instruction" not in str(payload)


def test_longhaul_state_and_projection(
    api_fixture: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    client, collab, longhaul, token = api_fixture
    task = _task(collab, "btk_state")
    longhaul.set_lane(task.id, "longhaul")
    longhaul.set_park_state(task.id, "waiting_review")
    longhaul.set_longhaul_json(task.id, {"acceptance": "tests pass", "workbook_path": "private"})
    attempt = longhaul.open_attempt(task.id, "cli-claude", "sonnet", session_id="ses_worker")
    longhaul.close_attempt(attempt["id"], "completed")

    state = call(client.get(f"/api/board/{task.id}/longhaul", headers=headers(token)))
    assert state.status_code == 200
    assert state.json()["acceptance"] == "tests pass"
    assert state.json()["park_state"] == "waiting_review"
    assert len(state.json()["attempts"]) == 1
    assert state.json()["current_worker"] is None

    board = call(client.get("/api/board", headers=headers(token)))
    row = next(row for row in board.json() if row["id"] == task.id)
    assert row["category_id"] is None
    assert row["lane"] == "longhaul"
    assert row["park_state"] == "waiting_review"
    assert row["attempt_count"] == 1


@pytest.mark.parametrize("path", ["/api/board/missing/conversation", "/api/board/missing/longhaul"])
def test_board_get_auth_and_not_found(api_fixture: Any, path: str) -> None:
    client, _, _, token = api_fixture
    assert call(client.get(path)).status_code == 401
    assert call(client.get(path, headers=headers(token))).status_code == 404


def test_archived_message_is_not_available(api_fixture: Any) -> None:
    client, collab, _, token = api_fixture
    task = _task(collab, "btk_archived")
    collab.update_board_task(task.id, {"archived_at": utc_now_iso()})
    response = call(
        client.post(
            f"/api/board/{task.id}/message",
            json={"content": "nope"},
            headers=headers(token),
        )
    )
    assert response.status_code == 404
