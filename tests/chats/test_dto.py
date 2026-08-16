"""ChatDTO (§3.1), extended PATCH (§3.2), grow-a-chat create (§3.3),
classify (§3.5) and plan seed (§3.6) contract tests."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omniagentos.api.main import app
from omniagentos.api.routes import intake as intake_routes
from omniagentos.chats.classify import classify_chat_project
from omniagentos.chats.store import ChatStore
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.conversations.store import ConversationStore
from omniagentos.db.store import SqliteStore
from omniagentos.projects import ProjectStore


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def project(store: SqliteStore) -> str:
    ProjectStore(store).create_project({"id": "proj_dto", "name": "DTO Project"})
    return "proj_dto"


def _create_chat(asgi_client: httpx.AsyncClient, **kwargs: Any) -> dict[str, Any]:
    resp = _run(asgi_client.post("/api/chats", json={"title": "DTO Chat", **kwargs}))
    assert resp.status_code == 201
    return resp.json()


def _mock_dispatch(calls: list[Any]) -> Any:
    def mock(store: Any, collab_store: Any, policy_cfg: Any, spec: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"session_id": "ses_dto", "execute": "session"}

    return mock


class TestChatDTO:
    def test_create_returns_flat_dto(
        self, asgi_client: httpx.AsyncClient, project: str
    ) -> None:
        chat = _create_chat(asgi_client, project_id=project, model="grok-4.5")
        assert chat["project_id"] == project
        assert chat["project_name"] == "DTO Project"
        assert chat["preferred_model"] == "grok-4.5"
        assert chat["orch_mode"] == "solo"
        assert chat["plan_mode"] is False
        assert chat["routing"] == {
            "allow": [],
            "deny": [],
            "speed": None,
            "effort": None,
            "hint": None,
        }
        assert chat["project_suggestion"] is None
        assert chat["message_count"] == 0
        assert chat["last_message_at"] is None
        assert chat["meta"]["preferred_model"] == "grok-4.5"

    def test_message_stats_join(
        self,
        asgi_client: httpx.AsyncClient,
        store: SqliteStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat = _create_chat(asgi_client)
        monkeypatch.setattr(
            "omniagentos.api.routes.chats.dispatch_spec", _mock_dispatch([])
        )
        resp = _run(
            asgi_client.post(
                f"/api/chats/{chat['id']}/messages", json={"content": "hello there"}
            )
        )
        assert resp.status_code == 201
        dto = _run(asgi_client.get(f"/api/chats/{chat['id']}")).json()
        assert dto["message_count"] == 1
        assert dto["last_message_at"] is not None
        listed = _run(asgi_client.get("/api/chats")).json()
        assert listed[0]["message_count"] == 1


class TestExtendedPatch:
    def test_model_patch_changes_next_dispatch(
        self,
        asgi_client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat = _create_chat(asgi_client)
        patch = _run(asgi_client.patch(f"/api/chats/{chat['id']}", json={"model": "sonnet-4"}))
        assert patch.status_code == 200
        assert patch.json()["preferred_model"] == "sonnet-4"

        calls: list[Any] = []
        monkeypatch.setattr(
            "omniagentos.api.routes.chats.dispatch_spec", _mock_dispatch(calls)
        )
        resp = _run(
            asgi_client.post(f"/api/chats/{chat['id']}/messages", json={"content": "hi"})
        )
        assert resp.status_code == 201
        assert calls[0]["model"] == "sonnet-4"

        # null clears back to auto
        patch2 = _run(asgi_client.patch(f"/api/chats/{chat['id']}", json={"model": None}))
        assert patch2.status_code == 200
        assert patch2.json()["preferred_model"] is None

    def test_project_patch_mirrors_companion_card(
        self,
        asgi_client: httpx.AsyncClient,
        collab_store: CollabStore,
        project: str,
    ) -> None:
        chat = _create_chat(asgi_client)
        patch = _run(
            asgi_client.patch(f"/api/chats/{chat['id']}", json={"project_id": project})
        )
        assert patch.status_code == 200
        assert patch.json()["project_id"] == project
        assert patch.json()["project_name"] == "DTO Project"
        card = collab_store.get_board_task(chat["board_task_id"])
        assert card is not None
        assert card["project_id"] == project

        # unassign mirrors the NULL too
        patch2 = _run(
            asgi_client.patch(f"/api/chats/{chat['id']}", json={"project_id": None})
        )
        assert patch2.status_code == 200
        assert patch2.json()["project_id"] is None
        card = collab_store.get_board_task(chat["board_task_id"])
        assert card["project_id"] is None

    def test_unknown_project_404(self, asgi_client: httpx.AsyncClient) -> None:
        chat = _create_chat(asgi_client)
        patch = _run(
            asgi_client.patch(
                f"/api/chats/{chat['id']}", json={"project_id": "proj_nope"}
            )
        )
        assert patch.status_code == 404

    def test_orch_plan_routing_patch(self, asgi_client: httpx.AsyncClient) -> None:
        chat = _create_chat(asgi_client)
        patch = _run(
            asgi_client.patch(
                f"/api/chats/{chat['id']}",
                json={
                    "orch_mode": "fanout",
                    "plan_mode": True,
                    "routing": {
                        "allow": ["grok"],
                        "deny": ["gpt"],
                        "speed": "fast",
                        "effort": "high",
                        "hint": "prefer grok for code",
                    },
                },
            )
        )
        assert patch.status_code == 200
        dto = patch.json()
        assert dto["orch_mode"] == "fanout"
        assert dto["plan_mode"] is True
        assert dto["routing"]["speed"] == "fast"
        assert dto["routing"]["allow"] == ["grok"]

        bad = _run(
            asgi_client.patch(f"/api/chats/{chat['id']}", json={"orch_mode": "bogus"})
        )
        assert bad.status_code == 400
        bad_speed = _run(
            asgi_client.patch(
                f"/api/chats/{chat['id']}", json={"routing": {"speed": "warp"}}
            )
        )
        assert bad_speed.status_code == 400


class TestGrowAChat:
    def test_link_existing_card(
        self,
        asgi_client: httpx.AsyncClient,
        collab_store: CollabStore,
        store: SqliteStore,
        project: str,
    ) -> None:
        card = BoardTask(title="Real board card", description="visible work")
        collab_store.create_board_task(card)
        collab_store.update_board_task(card.id, {"project_id": project})

        resp = _run(
            asgi_client.post(
                "/api/chats",
                json={"title": "Dock chat", "board_task_id": card.id},
            )
        )
        assert resp.status_code == 201
        chat = resp.json()
        assert chat["board_task_id"] == card.id
        # inherits the card's project
        assert chat["project_id"] == project
        # the card keeps its origin (stays a visible board card)
        row = collab_store.get_board_task(card.id)
        assert row["origin"] == "board"

    def test_unknown_card_404(self, asgi_client: httpx.AsyncClient) -> None:
        resp = _run(
            asgi_client.post(
                "/api/chats", json={"title": "Dock chat", "board_task_id": "btk_nope"}
            )
        )
        assert resp.status_code == 404

    def test_conflict_409(
        self, asgi_client: httpx.AsyncClient, collab_store: CollabStore
    ) -> None:
        first = _create_chat(asgi_client)
        resp = _run(
            asgi_client.post(
                "/api/chats",
                json={"title": "Second link", "board_task_id": first["board_task_id"]},
            )
        )
        assert resp.status_code == 409


class TestClassify:
    def _seed_chat_with_message(
        self, store: SqliteStore, project: str | None = None
    ) -> dict[str, Any]:
        chat = ChatStore(store).create_chat(title="Cascade routing question")
        ConversationStore(store).append(
            "chat", chat["id"], "user", "How does cascade routing pick models?"
        )
        return chat

    class _FakeClient:
        def __init__(self, payload: str) -> None:
            self.payload = payload

        def complete(self, *args: Any, **kwargs: Any) -> str:
            return self.payload

    def test_suggestion_stored_project_column_untouched(
        self, store: SqliteStore, project: str
    ) -> None:
        chat = self._seed_chat_with_message(store)
        client = self._FakeClient(
            '{"project_id": "proj_dto", "confidence": 0.9, "rationale": "routing talk"}'
        )
        suggestion = classify_chat_project(store, chat["id"], client=client)
        assert suggestion is not None
        assert suggestion["project_id"] == "proj_dto"
        assert suggestion["confidence"] == 0.9
        after = ChatStore(store).get_chat(chat["id"])
        # regression: classify NEVER writes chats.project_id
        assert after["project_id"] is None
        assert after["meta"]["project_suggestion"]["project_id"] == "proj_dto"
        assert after["meta"]["classified_at"]

    def test_below_threshold_discarded_but_stamped(
        self, store: SqliteStore, project: str
    ) -> None:
        chat = self._seed_chat_with_message(store)
        client = self._FakeClient(
            '{"project_id": "proj_dto", "confidence": 0.3, "rationale": "weak"}'
        )
        suggestion = classify_chat_project(store, chat["id"], client=client)
        assert suggestion is None
        after = ChatStore(store).get_chat(chat["id"])
        assert "project_suggestion" not in after["meta"]
        assert after["meta"]["classified_at"]

    def test_fires_once_per_chat(self, store: SqliteStore, project: str) -> None:
        chat = self._seed_chat_with_message(store)
        calls = []

        class CountingClient:
            def complete(self, *args: Any, **kwargs: Any) -> str:
                calls.append(1)
                return '{"project_id": "proj_dto", "confidence": 0.9, "rationale": "x"}'

        classify_chat_project(store, chat["id"], client=CountingClient())
        classify_chat_project(store, chat["id"], client=CountingClient())
        assert len(calls) == 1

    def test_route_returns_shape(
        self,
        asgi_client: httpx.AsyncClient,
        store: SqliteStore,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The route cannot pass ``client=``, so ``classify_chat_project`` builds a
        # real ``ShortCallClient`` — which POSTs to the local LiteLLM proxy on
        # :4000. Every OTHER test in this class injects the fake; this one did
        # not, so it was the one live model call in the offline lane (and the
        # failure was invisible: the LLM error is swallowed and the route still
        # answers 200 with nulls, so the shape assertion passed either way).
        # Stub the class the production seam constructs, and assert the VALUES
        # the route projects rather than only its keys.
        payload = '{"project_id": "proj_dto", "confidence": 0.9, "rationale": "routing talk"}'
        monkeypatch.setattr(
            "omniagentos.llm.client.ShortCallClient",
            lambda *a, **k: self._FakeClient(payload),
        )
        chat = self._seed_chat_with_message(store)
        resp = _run(asgi_client.post(f"/api/chats/{chat['id']}/classify", json={}))
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"project_id", "name", "confidence", "rationale"}
        assert body["project_id"] == "proj_dto"
        assert body["confidence"] == 0.9
        assert body["rationale"] == "routing talk"


class TestPlanSeed:
    def test_seed_records_job_and_defaults_goal(
        self,
        asgi_client: httpx.AsyncClient,
        store: SqliteStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat = _create_chat(asgi_client)
        monkeypatch.setattr(
            "omniagentos.api.routes.chats.dispatch_spec", _mock_dispatch([])
        )
        _run(
            asgi_client.post(
                f"/api/chats/{chat['id']}/messages",
                json={"content": "Build me a kanban dock"},
            )
        )

        # Prevent the background plan job from running a real planner.
        app.dependency_overrides[intake_routes.get_hierarchy_dal] = lambda: None
        app.dependency_overrides[intake_routes.get_planner_llm] = lambda: None
        app.dependency_overrides[intake_routes.get_router_llm] = lambda: None
        try:
            resp = _run(asgi_client.post(f"/api/chats/{chat['id']}/plan", json={}))
        finally:
            for dep in (
                intake_routes.get_hierarchy_dal,
                intake_routes.get_planner_llm,
                intake_routes.get_router_llm,
            ):
                app.dependency_overrides.pop(dep, None)
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "running"
        assert body["job_id"]

        after = ChatStore(store).get_chat(chat["id"])
        assert after["meta"]["plan_job_id"] == body["job_id"]

        # The seed used the last user message as the goal.
        job = intake_routes._plan_job_get(body["job_id"])
        assert job is not None
        assert "Build me a kanban dock" in job["goal"]


class TestSendEnvelope:
    def test_send_result_envelope(
        self,
        asgi_client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat = _create_chat(asgi_client)
        monkeypatch.setattr(
            "omniagentos.api.routes.chats.dispatch_spec", _mock_dispatch([])
        )
        resp = _run(
            asgi_client.post(
                f"/api/chats/{chat['id']}/messages", json={"content": "status?"}
            )
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["message"]["role"] == "user"
        assert body["message"]["content"] == "status?"
        assert body["dispatch"]["session_id"] == "ses_dto"
        assert body["dispatch"]["steered"] is False

    def test_fanout_send_returns_task_ids(
        self,
        asgi_client: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        chat = _create_chat(asgi_client)
        monkeypatch.setattr(
            "omniagentos.api.routes.chats.dispatch_spec", _mock_dispatch([])
        )
        resp = _run(
            asgi_client.post(
                f"/api/chats/{chat['id']}/messages",
                json={"content": "research competitors", "orch_mode": "fanout", "count": 3},
            )
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["message"]["role"] == "user"
        assert len(body["task_ids"]) == 3
