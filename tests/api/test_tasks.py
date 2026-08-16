from __future__ import annotations

import asyncio
import json

import httpx


def test_task_is_ready_and_carries_allowed_tools(
    asgi_client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = asyncio.run(
        asgi_client.post(
            "/api/tasks",
            headers=auth_headers,
            json={
                "title": "Summarise",
                "input": {"topic": "x"},
                "tools_allowed": ["shell"],
            },
        )
    )

    task = response.json()
    assert response.status_code == 201
    assert task["state"] == "ready"
    assert json.loads(task["input_json"])["tools_allowed"] == ["shell"]


def test_create_task_rejects_unknown_tool(
    asgi_client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = asyncio.run(
        asgi_client.post(
            "/api/tasks",
            headers=auth_headers,
            json={"title": "Unsafe", "tools_allowed": ["network"]},
        )
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation"


def test_create_task_requires_local_token(asgi_client: httpx.AsyncClient) -> None:
    # fix6: the control-plane create route is the create_task/create_run scope
    # escape — an unauthenticated (agent-reachable) caller is rejected 401.
    response = asyncio.run(
        asgi_client.post("/api/tasks", json={"title": "Escape", "tools_allowed": ["file_write"]})
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
