"""API tests for the intake routes (/api/intake/*, /api/board)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.api.routes.intake import get_clarify_llm
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.sessions.dal import SessionsDal


@pytest.fixture
def collab() -> CollabStore:
    return CollabStore(":memory:")


@pytest.fixture
def client(collab: CollabStore) -> httpx.AsyncClient:
    store = collab._store  # share one in-memory DB across both store deps.

    def _spec_llm(_prompt: str, _schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "mode": "spec",
            "spec": {
                "title": "Refined task",
                "description": "do it well",
                "acceptance_criteria": ["works"],
                "suggested_priority": "normal",
            },
        }

    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_collab_store] = lambda: collab
    app.dependency_overrides[get_clarify_llm] = lambda: _spec_llm
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


def test_clarify_returns_spec(client: httpx.AsyncClient) -> None:
    resp = asyncio.run(client.post("/api/intake/clarify", json={"draft": "make a dashboard"}))
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "spec"
    assert body["spec"]["title"] == "Refined task"


def test_clarify_requires_draft(client: httpx.AsyncClient) -> None:
    resp = asyncio.run(client.post("/api/intake/clarify", json={"draft": "   "}))
    assert resp.status_code == 400


def test_dispatch_creates_live_card_and_run(client: httpx.AsyncClient) -> None:
    resp = asyncio.run(
        client.post(
            "/api/intake/dispatch",
            json={
                "title": "Ship the feature",
                "description": "make it live",
                "acceptance_criteria": ["passes tests"],
                "suggested_priority": "high",
                "harness": "mock",
            },
        )
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["run_id"].startswith("run")
    assert data["task_id"].startswith("tsk")
    card = data["board_task"]
    assert card["status"] == "open"
    assert card["run_id"] == data["run_id"]
    assert card["priority"] == "high"

    board = asyncio.run(client.get("/api/board"))
    assert board.status_code == 200
    rows = board.json()
    match = next(t for t in rows if t["id"] == card["id"])
    # queued run reconciles to the To-Do column and exposes its live run state.
    assert match["status"] == "open"
    assert match["run_state"] == "queued"
    assert match["run_id"] == data["run_id"]


def test_cancel_board_task_requests_linked_session_cancel(
    client: httpx.AsyncClient,
    collab: CollabStore,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omniagentos.api.routes import sessions

    dal = SessionsDal(tmp_path / "cancel.db")
    dal.create_session(
        {
            "id": "ses_board_cancel",
            "source": "bridge",
            "project_dir": str(tmp_path),
            "state": "running",
            "model": "haiku",
        }
    )
    monkeypatch.setattr(sessions, "get_sessions_dal", lambda: dal)
    card = BoardTask(title="Cancel me")
    collab.create_board_task(card)
    collab.update_board_task(card.id, {"result_ref": "ses_board_cancel"})
    try:
        response = asyncio.run(client.post(f"/api/board/{card.id}/cancel"))
        repeated = asyncio.run(client.post(f"/api/board/{card.id}/cancel"))

        assert response.status_code == 200
        assert repeated.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert collab.get_board_task(card.id)["status"] == "cancelled"  # type: ignore[index]
        session = dal.get_session("ses_board_cancel")
        assert session is not None
        assert session["kill_requested"] == 1
        assert session["killed_by"] == "cancel_requested"
    finally:
        dal.close()


def test_dispatch_records_production_formation_selection(
    client: httpx.AsyncClient, collab: CollabStore
) -> None:
    resp = asyncio.run(
        client.post(
            "/api/intake/dispatch",
            json={
                "title": "Ship the feature",
                "description": "make it live",
                "acceptance_criteria": ["passes tests"],
                "suggested_priority": "high",
                "harness": "mock",
            },
        )
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "preflight" in data
    assert data["preflight"] is not None
    assert "formation_id" in data["preflight"]

    cursor = collab._store._connection.execute(
        "SELECT topology, source FROM formation_selections WHERE source = 'production'"
    )
    rows = cursor.fetchall()
    assert len(rows) > 0
    row = rows[0]
    assert row["source"] == "production"
    assert row["topology"] is not None
