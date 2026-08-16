"""HTTP routes test suite for Chats and hidden companion board tasks."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omniagentos.chats.store import ChatStore
from omniagentos.collab.store import CollabStore
from omniagentos.db.store import SqliteStore
from omniagentos.projects import ProjectStore


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def project(store: SqliteStore) -> str:
    ProjectStore(store).create_project({"id": "proj_routes", "name": "Routes Project"})
    return "proj_routes"


def test_chats_api_lifecycle(
    asgi_client: httpx.AsyncClient,
    collab_store: CollabStore,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ensure referenced project exists to satisfy FK constraint
    project_store = ProjectStore(store)
    project_store.create_project({"id": "proj_abc", "name": "ABC Project"})

    # 1. Create a chat
    create_resp = _run(
        asgi_client.post(
            "/api/chats",
            json={
                "title": "A New Chat Workspace",
                "project_id": "proj_abc",
                "meta": {"foo": "bar"},
            },
        )
    )
    assert create_resp.status_code == 201
    chat = create_resp.json()
    assert chat["id"].startswith("cht_")
    assert chat["title"] == "A New Chat Workspace"
    assert chat["project_id"] == "proj_abc"
    assert chat["status"] == "active"
    assert chat["meta"] == {"foo": "bar"}
    chat_id = chat["id"]
    board_task_id = chat["board_task_id"]

    # Verify that the hidden task is in board_tasks with origin='chat'
    task = collab_store.get_board_task(board_task_id)
    assert task is not None
    assert task["origin"] == "chat"

    # Verify that calling /api/board filters out this hidden task!
    board_resp = _run(asgi_client.get("/api/board"))
    assert board_resp.status_code == 200
    board_tasks = board_resp.json()
    assert not any(t["id"] == board_task_id for t in board_tasks)

    # 2. Get the chat
    get_resp = _run(asgi_client.get(f"/api/chats/{chat_id}"))
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == chat_id

    # 3. List chats
    list_resp = _run(asgi_client.get("/api/chats"))
    assert list_resp.status_code == 200
    chats = list_resp.json()
    assert len(chats) == 1
    assert chats[0]["id"] == chat_id

    list_filtered = _run(asgi_client.get("/api/chats?project_id=proj_abc"))
    assert len(list_filtered.json()) == 1

    list_empty = _run(asgi_client.get("/api/chats?project_id=proj_nonexistent"))
    assert len(list_empty.json()) == 0

    # 4. Update the chat
    patch_resp = _run(
        asgi_client.patch(
            f"/api/chats/{chat_id}",
            json={
                "title": "Altered Title",
                "status": "promoted",
                "meta": {"more": "data"},
            },
        )
    )
    assert patch_resp.status_code == 200
    updated = patch_resp.json()
    assert updated["title"] == "Altered Title"
    assert updated["status"] == "promoted"
    assert updated["promoted_at"] is not None
    assert updated["meta"] == {"foo": "bar", "more": "data"}

    # 5. Get 404 for nonexistent chat
    bad_resp = _run(asgi_client.get("/api/chats/cht_nonexistent"))
    assert bad_resp.status_code == 404


def test_post_message_dispatches_solo_agent_turn(
    asgi_client: httpx.AsyncClient,
    collab_store: CollabStore,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ensure referenced project exists to satisfy FK constraint
    project_store = ProjectStore(store)
    project_store.create_project({"id": "proj_123", "name": "123 Project"})

    # Create the chat workspace
    create_resp = _run(
        asgi_client.post(
            "/api/chats",
            json={"title": "Interactive Chat", "project_id": "proj_123"},
        )
    )
    assert create_resp.status_code == 201
    chat = create_resp.json()
    chat_id = chat["id"]

    # Mock the dispatch_spec call to prevent live agent execution / third-party provider calls
    dispatched_calls = []

    def mock_dispatch(
        store: Any, collab_store: Any, policy_cfg: Any, spec: Any, **kwargs: Any
    ) -> dict[str, Any]:
        dispatched_calls.append((spec, kwargs))
        return {"session_id": "ses_test123", "execute": "session"}

    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", mock_dispatch)

    # Post a message to the chat
    msg_resp = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/messages",
            json={"content": "Please write a summary of current tasks."},
        )
    )
    assert msg_resp.status_code == 201
    result = msg_resp.json()

    # Verify the message turn was saved
    msg = result["message"]
    assert msg["id"].startswith("cnv_")
    assert msg["role"] == "user"
    assert msg["content"] == "Please write a summary of current tasks."
    assert msg["scope_type"] == "chat"
    assert msg["scope_id"] == chat_id

    # Verify that dispatch_spec was called with the chat history-augmented prompt as description
    assert len(dispatched_calls) == 1
    spec, kwargs = dispatched_calls[0]
    assert spec.title == "Interactive Chat"
    assert "Please write a summary of current tasks." in spec.description
    assert kwargs["project_id"] == "proj_123"
    assert kwargs["execute"] == "session"
    assert kwargs["board_task_id"] == chat["board_task_id"]

    # Verify messages list endpoint
    list_resp = _run(asgi_client.get(f"/api/chats/{chat_id}/messages"))
    assert list_resp.status_code == 200
    messages = list_resp.json()
    assert len(messages) == 1
    assert messages[0]["content"] == "Please write a summary of current tasks."


def test_model_on_create_and_message(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model on CreateChatRequest stored as meta.preferred_model and threaded to dispatch."""
    project_store = ProjectStore(store)
    project_store.create_project({"id": "proj_model", "name": "Model Project"})

    create_resp = _run(
        asgi_client.post(
            "/api/chats",
            json={
                "title": "Model Chat",
                "project_id": "proj_model",
                "model": "gemini-3.6-flash",
            },
        )
    )
    assert create_resp.status_code == 201
    chat = create_resp.json()
    assert chat["meta"]["preferred_model"] == "gemini-3.6-flash"
    chat_id = chat["id"]

    dispatched_calls: list[tuple[Any, dict[str, Any]]] = []

    def mock_dispatch(
        store: Any, collab_store: Any, policy_cfg: Any, spec: Any, **kwargs: Any
    ) -> dict[str, Any]:
        dispatched_calls.append((spec, kwargs))
        return {"session_id": "ses_model", "execute": "session"}

    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", mock_dispatch)

    # Post message WITHOUT model override — should use chat-level preferred_model
    msg_resp = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/messages",
            json={"content": "Use the default model."},
        )
    )
    assert msg_resp.status_code == 201
    assert len(dispatched_calls) == 1
    _, kwargs = dispatched_calls[0]
    assert kwargs["model"] == "gemini-3.6-flash"

    # Post message WITH model override
    dispatched_calls.clear()
    msg_resp2 = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/messages",
            json={"content": "Override model.", "model": "grok-4.5"},
        )
    )
    assert msg_resp2.status_code == 201
    assert len(dispatched_calls) == 1
    _, kwargs2 = dispatched_calls[0]
    assert kwargs2["model"] == "grok-4.5"


def test_folders(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
) -> None:
    """GET /api/chats/folders returns the color registry; folder filter works."""
    project_store = ProjectStore(store)
    project_store.create_project({"id": "proj_folder", "name": "Folder Project"})

    # Create chats in different folders
    for title, folder in [("C1", "design"), ("C2", "design"), ("C3", "backend"), ("C4", None)]:
        meta = {"folder": folder} if folder else None
        resp = _run(
            asgi_client.post(
                "/api/chats",
                json={"title": title, "project_id": "proj_folder", "meta": meta},
            )
        )
        assert resp.status_code == 201

    # GET /api/chats/folders — 088 contract: {folders: [{name, color, chat_count}]}
    folders_resp = _run(asgi_client.get("/api/chats/folders"))
    assert folders_resp.status_code == 200
    folders = folders_resp.json()
    assert "folders" in folders
    by_name = {f["name"]: f for f in folders["folders"]}
    assert sorted(by_name) == ["backend", "design"]
    # Unregistered folders (free text on chats only) default to gray
    assert by_name["design"] == {"name": "design", "color": "gray", "chat_count": 2}
    assert by_name["backend"] == {"name": "backend", "color": "gray", "chat_count": 1}

    # Filter by folder
    design_resp = _run(asgi_client.get("/api/chats?folder=design"))
    assert design_resp.status_code == 200
    assert len(design_resp.json()) == 2

    backend_resp = _run(asgi_client.get("/api/chats?folder=backend"))
    assert len(backend_resp.json()) == 1

    # No-folder filter
    no_folder_resp = _run(asgi_client.get("/api/chats?folder="))
    assert len(no_folder_resp.json()) == 1

    # PATCH folder
    chat_id = _run(asgi_client.get("/api/chats?folder=backend")).json()[0]["id"]
    patch_resp = _run(
        asgi_client.patch(f"/api/chats/{chat_id}", json={"folder": "frontend"})
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["meta"]["folder"] == "frontend"


def test_folder_color_registry_routes(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
) -> None:
    """POST /folders/{name}/color registers + recolors; invalid tokens 400."""
    # Creation path: color on an unknown name registers an empty folder
    create = _run(
        asgi_client.post("/api/chats/folders/Research/color", json={"color": "teal"})
    )
    assert create.status_code == 200
    assert create.json()["folder"] == {"name": "Research", "color": "teal", "chat_count": 0}

    # The empty registered folder is listed
    listed = _run(asgi_client.get("/api/chats/folders")).json()["folders"]
    assert {"name": "Research", "color": "teal", "chat_count": 0} in listed

    # Recolor an existing registry row
    recolor = _run(
        asgi_client.post("/api/chats/folders/Research/color", json={"color": "violet"})
    )
    assert recolor.status_code == 200
    assert recolor.json()["folder"]["color"] == "violet"

    # Hex (or any non-token) is rejected — the API stores token names only
    bad = _run(
        asgi_client.post("/api/chats/folders/Research/color", json={"color": "#ff0000"})
    )
    assert bad.status_code == 400

    # Names containing '/' cannot be registered: an encoded slash decodes
    # into a path separator and never routes (the store validator guards the
    # JSON-body variant, see test_set_folder_color_validates).
    bad_name = _run(
        asgi_client.post("/api/chats/folders/a%2Fb/color", json={"color": "red"})
    )
    assert bad_name.status_code == 404


def test_folder_rename_route(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
) -> None:
    """POST /folders/{name}/rename moves chats and re-keys the registry row."""
    for title in ("R1", "R2"):
        resp = _run(
            asgi_client.post(
                "/api/chats", json={"title": title, "meta": {"folder": "ops"}}
            )
        )
        assert resp.status_code == 201
    color = _run(asgi_client.post("/api/chats/folders/ops/color", json={"color": "green"}))
    assert color.status_code == 200

    rename = _run(
        asgi_client.post("/api/chats/folders/ops/rename", json={"new_name": "operations"})
    )
    assert rename.status_code == 200
    # Registry row re-keyed: color survives the rename
    assert rename.json()["folder"] == {
        "name": "operations",
        "color": "green",
        "chat_count": 2,
    }

    # Every chat moved: the old name filters to nothing
    assert _run(asgi_client.get("/api/chats?folder=ops")).json() == []
    moved = _run(asgi_client.get("/api/chats?folder=operations")).json()
    assert {c["title"] for c in moved} == {"R1", "R2"}

    # The old name is gone from the registry listing
    names = [f["name"] for f in _run(asgi_client.get("/api/chats/folders")).json()["folders"]]
    assert "ops" not in names
    assert "operations" in names

    # Unknown source folder → 404
    missing = _run(
        asgi_client.post("/api/chats/folders/nope/rename", json={"new_name": "x"})
    )
    assert missing.status_code == 404


def test_folder_delete_route(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
) -> None:
    """DELETE /folders/{name}: chats fall back to the Inbox, registry row goes."""
    resp = _run(
        asgi_client.post("/api/chats", json={"title": "D1", "meta": {"folder": "temp"}})
    )
    assert resp.status_code == 201
    chat_id = resp.json()["id"]
    _run(asgi_client.post("/api/chats/folders/temp/color", json={"color": "red"}))

    deleted = _run(asgi_client.delete("/api/chats/folders/temp"))
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": "temp", "chats_moved": 1}

    # Chat fell back to no-folder (Inbox); folder no longer listed
    chat = _run(asgi_client.get(f"/api/chats/{chat_id}")).json()
    assert "folder" not in chat["meta"]
    names = [f["name"] for f in _run(asgi_client.get("/api/chats/folders")).json()["folders"]]
    assert "temp" not in names

    # Deleting it again → 404
    again = _run(asgi_client.delete("/api/chats/folders/temp"))
    assert again.status_code == 404


def test_soft_delete(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
) -> None:
    """DELETE /api/chats/{id} soft-deletes; excluded from lists; GET still works."""
    project_store = ProjectStore(store)
    project_store.create_project({"id": "proj_del", "name": "Delete Project"})

    create_resp = _run(
        asgi_client.post(
            "/api/chats",
            json={"title": "To Delete", "project_id": "proj_del"},
        )
    )
    assert create_resp.status_code == 201
    chat_id = create_resp.json()["id"]

    # Delete
    del_resp = _run(asgi_client.delete(f"/api/chats/{chat_id}"))
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "deleted"

    # Excluded from list
    list_resp = _run(asgi_client.get("/api/chats"))
    assert not any(c["id"] == chat_id for c in list_resp.json())

    # Still accessible via GET
    get_resp = _run(asgi_client.get(f"/api/chats/{chat_id}"))
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "deleted"

    # 404 on deleting non-existent
    bad_del = _run(asgi_client.delete("/api/chats/cht_nonexistent"))
    assert bad_del.status_code == 404


def test_spawn_creates_nested_tasks(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    collab_store: CollabStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/chats/{id}/spawn creates child tasks nested under companion task."""
    project_store = ProjectStore(store)
    project_store.create_project({"id": "proj_spawn", "name": "Spawn Project"})

    create_resp = _run(
        asgi_client.post(
            "/api/chats",
            json={"title": "Spawn Chat", "project_id": "proj_spawn"},
        )
    )
    assert create_resp.status_code == 201
    chat = create_resp.json()
    chat_id = chat["id"]
    companion_btk = chat["board_task_id"]

    dispatched_tasks: list[str] = []

    def mock_dispatch(
        store: Any, collab_store: Any, policy_cfg: Any, spec: Any, **kwargs: Any
    ) -> dict[str, Any]:
        dispatched_tasks.append(kwargs.get("board_task_id", ""))
        return {"session_id": "ses_spawn", "execute": "session"}

    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", mock_dispatch)

    # Spawn 3 sub-agents
    spawn_resp = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/spawn",
            json={"goal": "Implement feature X", "count": 3},
        )
    )
    assert spawn_resp.status_code == 201
    result = spawn_resp.json()
    assert "task_ids" in result
    assert len(result["task_ids"]) == 3

    # Verify each spawned task exists and is nested under the companion task
    for tid in result["task_ids"]:
        bt = collab_store.get_board_task(tid)
        assert bt is not None
        assert bt["origin"] == "chat"
        org = bt.get("org_json") or bt.get("org")
        if isinstance(org, str):
            import json
            org = json.loads(org)
        assert org.get("parent_task_id") == companion_btk

    # Verify dispatched with correct board_task_ids
    assert dispatched_tasks == result["task_ids"]

    # P0-9: spawned sub-tasks ARE visible on /api/board now — only companion
    # cards (pointed at by a chat) stay hidden. They land in the chat's project.
    board_resp = _run(asgi_client.get("/api/board"))
    board_ids = {t["id"] for t in board_resp.json()}
    for tid in result["task_ids"]:
        assert tid in board_ids
    # The companion task itself stays hidden.
    assert companion_btk not in board_ids
    # Spawned cards carry the chat's project (server-side scope filter agrees).
    scoped = _run(asgi_client.get("/api/board?project_id=proj_spawn"))
    scoped_ids = {t["id"] for t in scoped.json()}
    for tid in result["task_ids"]:
        assert tid in scoped_ids
    # ...and the parent disclosure returns them as children of the companion.
    children = _run(asgi_client.get(f"/api/board?parent_task_id={companion_btk}"))
    child_ids = {t["id"] for t in children.json()}
    assert set(result["task_ids"]) <= child_ids


def test_promote_returns_task_ids(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/chats/{id}/promote extracts action items and returns task ids."""
    project_store = ProjectStore(store)
    project_store.create_project({"id": "proj_promote", "name": "Promote Project"})

    create_resp = _run(
        asgi_client.post(
            "/api/chats",
            json={"title": "Promote Chat", "project_id": "proj_promote"},
        )
    )
    assert create_resp.status_code == 201
    chat = create_resp.json()
    chat_id = chat["id"]

    posted_dispatch: list[dict[str, Any]] = []

    def mock_dispatch(
        store: Any, collab_store: Any, policy_cfg: Any, spec: Any, **kwargs: Any
    ) -> dict[str, Any]:
        btk = kwargs.get("board_task_id") or f"btk_{len(posted_dispatch)}"
        posted_dispatch.append({"board_task_id": btk, "spec": spec})
        return {
            "board_task": {"id": btk},
            "task_id": f"tsk_{len(posted_dispatch)}",
        }

    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", mock_dispatch)

    # Mock the LLM client to avoid real API calls
    def mock_complete(self_: Any, *args: Any, **kwargs: Any) -> str:
        return (
            '{"items": ['
            '{"title": "Build the widget", "description": "Implement the main UI widget"},'
            '{"title": "Write tests", "description": "Add unit tests for the widget"}'
            ']}'
        )

    monkeypatch.setattr(
        "omniagentos.llm.client.ShortCallClient.complete", mock_complete
    )

    # Add some messages to the chat
    _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/messages",
            json={"content": "We need to build a new widget."},
        )
    )

    # Promote
    promote_resp = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/promote",
            json={"project_id": "proj_promote"},
        )
    )
    assert promote_resp.status_code == 201
    result = promote_resp.json()
    assert "project_id" in result
    assert result["project_id"] == "proj_promote"
    assert "task_ids" in result
    assert len(result["task_ids"]) == 2

    # Chat status should be 'promoted'
    chat_resp = _run(asgi_client.get(f"/api/chats/{chat_id}"))
    assert chat_resp.json()["status"] == "promoted"
    assert chat_resp.json()["promoted_at"] is not None


def test_promote_empty_chat_fails(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
) -> None:
    """Promoting an empty chat returns 400."""
    project_store = ProjectStore(store)
    project_store.create_project({"id": "proj_empty", "name": "Empty Project"})

    create_resp = _run(
        asgi_client.post(
            "/api/chats",
            json={"title": "Empty Chat", "project_id": "proj_empty"},
        )
    )
    chat_id = create_resp.json()["id"]

    promote_resp = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/promote",
            json={"project_id": "proj_empty"},
        )
    )
    assert promote_resp.status_code == 400


def test_attachment_validation(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attachment manifest on messages is validated."""
    project_store = ProjectStore(store)
    project_store.create_project({"id": "proj_att", "name": "Attachment Project"})

    create_resp = _run(
        asgi_client.post(
            "/api/chats",
            json={"title": "Attachment Chat", "project_id": "proj_att"},
        )
    )
    chat_id = create_resp.json()["id"]

    def mock_dispatch(
        store: Any, collab_store: Any, policy_cfg: Any, spec: Any, **kwargs: Any
    ) -> dict[str, Any]:
        return {"session_id": "ses_att", "execute": "session"}

    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", mock_dispatch)

    # Valid attachments
    ok_resp = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/messages",
            json={
                "content": "Check this skill.",
                "meta": {
                    "attachments": [
                        {"kind": "skill", "ref": "skill_abc", "label": "My Skill"},
                        {"kind": "file", "ref": "/tmp/doc.pdf", "label": "Doc"},
                    ]
                },
            },
        )
    )
    assert ok_resp.status_code == 201
    msg = ok_resp.json()["message"]
    assert len(msg["meta"]["attachments"]) == 2

    # Invalid kind
    bad_resp = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/messages",
            json={
                "content": "Bad attachment.",
                "meta": {
                    "attachments": [
                        {"kind": "invalid", "ref": "x", "label": "Bad"}
                    ]
                },
            },
        )
    )
    assert bad_resp.status_code == 400

    # Missing ref
    bad_resp2 = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/messages",
            json={
                "content": "Missing ref.",
                "meta": {
                    "attachments": [
                        {"kind": "skill", "label": "Missing Ref"}
                    ]
                },
            },
        )
    )
    assert bad_resp2.status_code == 400


def test_chat_updated_sse_emitted_on_first_message_title_rename(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Q-FIX-01: first-message title rename must emit chat.updated SSE event."""

    def _mock_dispatch(store: Any, collab_store: Any, policy_cfg: Any, spec: Any, **kwargs: Any) -> dict[str, Any]:
        return {"session_id": "ses_q01", "execute": "session"}

    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", _mock_dispatch)

    # Create a chat whose title is exactly "New chat" (the trigger)
    create_resp = _run(asgi_client.post("/api/chats", json={"title": "New chat"}))
    assert create_resp.status_code == 201
    chat = create_resp.json()
    chat_id = chat["id"]

    # Send the first message — the title hook renames the chat and should emit
    # a chat.updated SSE event so other tabs / clients can refresh.
    send_resp = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/messages",
            json={"content": "Designing the kanban dock"},
        )
    )
    assert send_resp.status_code == 201

    # The title rename should have persisted a chat.updated event.
    rows = store._connection.execute(
        "SELECT type, target_type, target_id, payload_json FROM events WHERE type = 'chat.updated'"
    ).fetchall()
    assert len(rows) >= 1
    evt = rows[0]
    assert evt["target_type"] == "chat"
    assert evt["target_id"] == chat_id
    import json as _js
    payload = _js.loads(evt["payload_json"])
    assert payload["chat_id"] == chat_id
    assert payload["ts"]


def test_emit_chat_updated_helper_writes_correct_event(
    store: SqliteStore,
) -> None:
    """Q-FIX-01: _emit_chat_updated writes the pinned SSE event shape."""
    import json as _js

    from omniagentos.api.routes.chats import _emit_chat_updated

    chat_id = "cht_event_test"
    ChatStore(store).create_chat(title="Event Test Chat")
    _emit_chat_updated(store, chat_id)

    rows = store._connection.execute(
        "SELECT type, target_type, target_id, payload_json FROM events WHERE type = 'chat.updated'"
    ).fetchall()
    assert len(rows) >= 1
    evt = rows[-1]
    assert evt["type"] == "chat.updated"
    assert evt["target_type"] == "chat"
    assert evt["target_id"] == chat_id
    payload = _js.loads(evt["payload_json"])
    assert payload["chat_id"] == chat_id
    assert "ts" in payload


def test_classify_hook_emits_chat_updated(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Q-FIX-01: background classify must emit chat.updated when suggestion lands."""
    import json as _js
    import time as _time

    def _mock_dispatch(store: Any, collab_store: Any, policy_cfg: Any, spec: Any, **kwargs: Any) -> dict[str, Any]:
        return {"session_id": "ses_classify", "execute": "session"}

    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", _mock_dispatch)

    # Patch classify to return a fake suggestion without needing an LLM.
    def _sync_classify(s: Any, chat_id: str, **kwargs: Any) -> dict[str, Any] | None:
        chat_store = ChatStore(s)
        suggestion = {
            "project_id": project,
            "name": "DTO Project",
            "confidence": 0.9,
            "rationale": "test",
        }
        meta = dict(chat_store.get_chat(chat_id).get("meta") or {})
        meta["project_suggestion"] = suggestion
        meta["classified_at"] = "2025-01-01T00:00:00Z"
        chat_store.update_chat(chat_id, meta=meta)
        return suggestion

    monkeypatch.setattr(
        "omniagentos.chats.classify.classify_chat_project",
        _sync_classify,
    )

    # Create chat with "New chat" title and no project (triggers classify)
    create_resp = _run(asgi_client.post("/api/chats", json={"title": "New chat"}))
    assert create_resp.status_code == 201
    chat = create_resp.json()
    chat_id = chat["id"]

    # Send first message — triggers title rename + classify (background thread).
    send_resp = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/messages",
            json={"content": "How does cascade routing work?"},
        )
    )
    assert send_resp.status_code == 201

    # Wait briefly for the classify background thread to complete.
    deadline = _time.monotonic() + 5.0
    while _time.monotonic() < deadline:
        rows = store._connection.execute(
            "SELECT COUNT(*) AS n FROM events WHERE type = 'chat.updated' AND target_id = ?",
            (chat_id,),
        ).fetchone()
        if rows and rows["n"] >= 2:
            break
        _time.sleep(0.1)

    rows = store._connection.execute(
        "SELECT payload_json FROM events WHERE type = 'chat.updated' AND target_id = ?",
        (chat_id,),
    ).fetchall()
    assert len(rows) >= 2, f"expected at least 2 chat.updated events (title + classify), got {len(rows)}"
    for row in rows:
        payload = _js.loads(row["payload_json"])
        assert payload["chat_id"] == chat_id
        assert "ts" in payload


def test_classify_hook_emits_chat_updated_no_project_no_project_id(
    asgi_client: httpx.AsyncClient,
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Q-FIX-01: classify fires event even when chat already has project_id set
    (classify skipped — only the title rename should emit once)."""

    def _mock_dispatch(store: Any, collab_store: Any, policy_cfg: Any, spec: Any, **kwargs: Any) -> dict[str, Any]:
        return {"session_id": "ses_skipclass", "execute": "session"}

    monkeypatch.setattr("omniagentos.api.routes.chats.dispatch_spec", _mock_dispatch)

    # Chat with a project_id already set — classify guard skips.
    ProjectStore(store).create_project({"id": "proj_existing", "name": "Existing"})
    create_resp = _run(
        asgi_client.post(
            "/api/chats", json={"title": "New chat", "project_id": "proj_existing"}
        )
    )
    assert create_resp.status_code == 201
    chat = create_resp.json()
    chat_id = chat["id"]

    send_resp = _run(
        asgi_client.post(
            f"/api/chats/{chat_id}/messages",
            json={"content": "Some message here"},
        )
    )
    assert send_resp.status_code == 201

    # Only the title rename should have emitted chat.updated (classify is skipped).
    rows = store._connection.execute(
        "SELECT payload_json FROM events WHERE type = 'chat.updated' AND target_id = ?",
        (chat_id,),
    ).fetchall()
    assert len(rows) == 1, f"expected exactly 1 chat.updated (title only), got {len(rows)}"
