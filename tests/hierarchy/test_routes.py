from __future__ import annotations

import asyncio

import httpx

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _create_project(client: httpx.AsyncClient, name: str) -> str:
    resp = _run(client.post("/api/projects", json={"name": name}))
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _make_task(store: SqliteStore, project_id: str, title: str, state: str) -> str:
    task_id = new_id("tsk")
    now = utc_now_iso()
    store.create_task(
        {
            "id": task_id,
            "project_id": project_id,
            "title": title,
            "state": state,
            "created_at": now,
            "updated_at": now,
        }
    )
    return task_id


def test_tree_endpoint_nests_two_subprojects_and_tasks(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    root = _create_project(asgi_client, "Root")
    # Sub-projects are created through the store seam the client shares, matching
    # how the planner will attach them; the tree endpoint is the read surface.
    from omniagentos.projects import ProjectStore

    projects = ProjectStore(store)
    sub_a = projects.create_subproject(root, {"name": "SubA"})["id"]
    sub_b = projects.create_subproject(root, {"name": "SubB"})["id"]
    _make_task(store, root, "root-task", "running")
    _make_task(store, sub_a, "a-task", "completed")

    resp = _run(asgi_client.get("/api/projects/tree"))
    assert resp.status_code == 200, resp.text
    tree = resp.json()
    assert [n["project"]["id"] for n in tree] == [root]
    root_node = tree[0]
    assert root_node["status"] == "active"
    assert [t["title"] for t in root_node["tasks"]] == ["root-task"]
    assert {n["project"]["id"] for n in root_node["sub_projects"]} == {sub_a, sub_b}
    # "tree" must not be swallowed by the /api/projects/{id} detail route.
    assert resp.headers["content-type"].startswith("application/json")


def test_project_conversation_append_and_read(asgi_client: httpx.AsyncClient) -> None:
    project_id = _create_project(asgi_client, "Chatty")

    posted = _run(
        asgi_client.post(
            f"/api/projects/{project_id}/message",
            json={"role": "user", "content": "kick off the plan"},
        )
    )
    assert posted.status_code == 201, posted.text
    assert posted.json()["seq"] == 1

    _run(
        asgi_client.post(
            f"/api/projects/{project_id}/message",
            json={"role": "agent", "content": "on it", "model": "fable"},
        )
    )

    convo = _run(asgi_client.get(f"/api/projects/{project_id}/conversation"))
    assert convo.status_code == 200
    messages = convo.json()
    assert [m["content"] for m in messages] == ["kick off the plan", "on it"]
    assert messages[1]["model"] == "fable"


def test_project_message_unknown_project_is_404(asgi_client: httpx.AsyncClient) -> None:
    resp = _run(
        asgi_client.post(
            "/api/projects/proj_missing/message",
            json={"role": "user", "content": "hi"},
        )
    )
    assert resp.status_code == 404


def test_task_conversation_append_and_read(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    project_id = _create_project(asgi_client, "Holder")
    task_id = _make_task(store, project_id, "do the thing", "draft")

    posted = _run(
        asgi_client.post(
            f"/api/tasks/{task_id}/message",
            json={"role": "system", "content": "task created"},
        )
    )
    assert posted.status_code == 201, posted.text

    convo = _run(asgi_client.get(f"/api/tasks/{task_id}/conversation"))
    assert convo.status_code == 200
    messages = convo.json()
    assert [m["role"] for m in messages] == ["system"]
    assert messages[0]["scope_type"] == "task"


def test_task_conversation_unknown_task_is_404(asgi_client: httpx.AsyncClient) -> None:
    resp = _run(asgi_client.get("/api/tasks/tsk_missing/conversation"))
    assert resp.status_code == 404
