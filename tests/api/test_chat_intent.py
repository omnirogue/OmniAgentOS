"""API tests for INTENT-1 suggest route + agreement logging on send."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_policy_config, get_store
from omniagentos.api.main import app
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.chats.intent import (
    AGREEMENT_EVENT_TYPE,
    PROMOTION_SHADOW,
    load_promotion_config,
)
from omniagentos.collab.store import CollabStore
from omniagentos.db.store import SqliteStore
from omniagentos.policy import PolicyConfig, load_policy
from omniagentos.projects import ProjectStore


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "chat_intent_api.db"


@pytest.fixture
def collab_store(tmp_db_path: Path) -> CollabStore:
    return CollabStore(str(tmp_db_path))


@pytest.fixture
def store(collab_store: CollabStore) -> SqliteStore:
    return collab_store._store


@pytest.fixture
def policy_config() -> PolicyConfig:
    return load_policy()


@pytest.fixture
def asgi_client(
    store: SqliteStore,
    collab_store: CollabStore,
    policy_config: PolicyConfig,
) -> Iterator[httpx.AsyncClient]:
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_collab_store] = lambda: collab_store
    app.dependency_overrides[get_policy_config] = lambda: policy_config
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def chat_id(asgi_client: httpx.AsyncClient, store: SqliteStore) -> str:
    ProjectStore(store).create_project({"id": "proj_intent", "name": "Intent Project"})
    resp = _run(
        asgi_client.post(
            "/api/chats",
            json={"title": "Intent API chat", "project_id": "proj_intent"},
        )
    )
    assert resp.status_code == 201
    return str(resp.json()["id"])


class _FakeLLM:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def complete(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return json.dumps(self.payload)


def test_intent_suggest_returns_shape_and_shadow(
    asgi_client: httpx.AsyncClient,
    chat_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INTENT-E1: suggest returns {intent, confidence, promotion}; shadow without bar."""

    def _fake_classify(message: str, **kwargs: Any) -> dict[str, Any]:
        return {"intent": "project", "confidence": 0.97, "rationale": "work"}

    monkeypatch.setattr(
        "omniagentos.chats.intent.classify_chat_intent",
        _fake_classify,
    )

    resp = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/intent/suggest",
            json={"message": "implement the billing endpoint"},
        )
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) >= {"intent", "confidence", "promotion"}
    assert body["intent"] == "project"
    assert body["confidence"] == 0.97
    # No agreement history → always shadow, regardless of confidence.
    assert body["promotion"] == PROMOTION_SHADOW


def test_intent_suggest_404(asgi_client: httpx.AsyncClient) -> None:
    resp = _run(
        asgi_client.post(
            "/api/chats/cht_missing/intent/suggest",
            json={"message": "hi"},
        )
    )
    assert resp.status_code == 404


def test_intent_suggest_never_mutates_project_or_routines(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    chat_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INTENT-E1 far side: chats.project_id + routines unchanged by suggest."""

    def _fake_classify(message: str, **kwargs: Any) -> dict[str, Any]:
        return {"intent": "loop", "confidence": 0.99, "rationale": "recurring"}

    monkeypatch.setattr(
        "omniagentos.chats.intent.classify_chat_intent",
        _fake_classify,
    )

    before_chat = store._connection.execute(
        "SELECT id, project_id, status, meta_json FROM chats ORDER BY id"
    ).fetchall()
    before_routines = store._connection.execute(
        "SELECT id, status FROM routines ORDER BY id"
    ).fetchall()

    resp = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/intent/suggest",
            json={"message": "every day at 9 check the queue"},
        )
    )
    assert resp.status_code == 200
    assert resp.json()["promotion"] == PROMOTION_SHADOW

    after_chat = store._connection.execute(
        "SELECT id, project_id, status, meta_json FROM chats ORDER BY id"
    ).fetchall()
    after_routines = store._connection.execute(
        "SELECT id, status FROM routines ORDER BY id"
    ).fetchall()
    assert after_chat == before_chat
    assert after_routines == before_routines


def test_agreement_logged_on_send_far_side(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    chat_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INTENT-E2: send carrying suggestion inserts agreement event; not via handler return."""

    def mock_dispatch(
        store: Any, collab_store: Any, policy_cfg: Any, spec: Any, **kwargs: Any
    ) -> dict[str, Any]:
        return {"session_id": "ses_intent", "execute": "session"}

    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", mock_dispatch)

    before_ids = {
        row["id"]
        for row in store.get_events_after(0, types=[AGREEMENT_EVENT_TYPE], limit=100)
    }

    resp = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/messages",
            json={
                "content": "let's keep this as a plain chat",
                "meta": {
                    "suggested_intent": "project",
                    "chosen_intent": "chat",
                },
            },
        )
    )
    assert resp.status_code == 201
    # Handler return is message/dispatch — agreement is far-side events only.
    body = resp.json()
    assert "message" in body
    assert "suggested_intent" not in body
    assert "chosen_intent" not in body

    rows = store.get_events_after(0, types=[AGREEMENT_EVENT_TYPE], limit=100)
    new_rows = [r for r in rows if r["id"] not in before_ids]
    assert len(new_rows) == 1
    payload = json.loads(new_rows[0]["payload_json"])
    assert payload["suggested_intent"] == "project"
    assert payload["chosen_intent"] == "chat"
    assert new_rows[0]["target_type"] == "chat"
    assert new_rows[0]["target_id"] == chat_id


def test_agreement_log_failure_does_not_block_send(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    chat_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INTENT-E2: logging failure never blocks the send (P8 idiom)."""

    def mock_dispatch(
        store: Any, collab_store: Any, policy_cfg: Any, spec: Any, **kwargs: Any
    ) -> dict[str, Any]:
        return {"session_id": "ses_intent2", "execute": "session"}

    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", mock_dispatch)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("events table on fire")

    monkeypatch.setattr("omniagentos.chats.intent.log_agreement", _boom)

    resp = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/messages",
            json={
                "content": "still send me",
                "meta": {
                    "suggested_intent": "chat",
                    "chosen_intent": "chat",
                },
            },
        )
    )
    assert resp.status_code == 201
    assert resp.json()["message"]["content"] == "still send me"


def test_send_without_intent_meta_skips_agreement(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    chat_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mock_dispatch(
        store: Any, collab_store: Any, policy_cfg: Any, spec: Any, **kwargs: Any
    ) -> dict[str, Any]:
        return {"session_id": "ses_intent3", "execute": "session"}

    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", mock_dispatch)

    before = len(store.get_events_after(0, types=[AGREEMENT_EVENT_TYPE], limit=100))
    resp = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/messages",
            json={"content": "no intent meta here"},
        )
    )
    assert resp.status_code == 201
    after = len(store.get_events_after(0, types=[AGREEMENT_EVENT_TYPE], limit=100))
    assert after == before


def test_promotion_config_file_is_loaded() -> None:
    cfg = load_promotion_config()
    assert cfg["loop"]["min_agreement"] == 0.95
    assert cfg["demote_below"] == 0.80
