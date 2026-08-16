from __future__ import annotations

import asyncio
import json

import httpx

from tests.api.fake_store import FakeStore


async def _create_task(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> dict[str, object]:
    response = await client.post(
        "/api/tasks", headers=auth_headers, json={"title": "Implement endpoint"}
    )
    assert response.status_code == 201
    return response.json()


def test_run_uses_single_agent_default_plan(
    asgi_client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    task = asyncio.run(_create_task(asgi_client, auth_headers))
    response = asyncio.run(
        asgi_client.post(
            f"/api/tasks/{task['id']}/runs", headers=auth_headers, json={"harness": "mock"}
        )
    )

    assert response.status_code == 201
    assert json.loads(response.json()["plan_json"]) == [
        {"name": "agent", "kind": "agent", "action_class": "sandboxed_creation", "params": {}}
    ]


def test_run_top_level_prompt_reaches_agent_step_params(
    asgi_client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    task = asyncio.run(_create_task(asgi_client, auth_headers))
    response = asyncio.run(
        asgi_client.post(
            f"/api/tasks/{task['id']}/runs",
            headers=auth_headers,
            json={"harness": "mock", "prompt": "write the brief"},
        )
    )

    assert response.status_code == 201
    plan = json.loads(response.json()["plan_json"])
    assert plan[0]["params"]["prompt"] == "write the brief"


def test_run_explicit_step_prompt_wins_over_top_level(
    asgi_client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    task = asyncio.run(_create_task(asgi_client, auth_headers))
    response = asyncio.run(
        asgi_client.post(
            f"/api/tasks/{task['id']}/runs",
            headers=auth_headers,
            json={
                "harness": "mock",
                "prompt": "top-level prompt",
                "plan": [{"name": "agent", "kind": "agent", "params": {"prompt": "step prompt"}}],
            },
        )
    )

    assert response.status_code == 201
    plan = json.loads(response.json()["plan_json"])
    assert plan[0]["params"]["prompt"] == "step prompt"


def test_run_rejects_interleaved_validate_plan(
    asgi_client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    task = asyncio.run(_create_task(asgi_client, auth_headers))
    response = asyncio.run(
        asgi_client.post(
            f"/api/tasks/{task['id']}/runs",
            headers=auth_headers,
            json={
                "harness": "mock",
                "plan": [
                    {"name": "check", "kind": "validate"},
                    {"name": "agent", "kind": "agent"},
                ],
            },
        )
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation"


def test_run_accepts_all_validate_plan(
    asgi_client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    task = asyncio.run(_create_task(asgi_client, auth_headers))
    response = asyncio.run(
        asgi_client.post(
            f"/api/tasks/{task['id']}/runs",
            headers=auth_headers,
            json={"harness": "mock", "plan": [{"name": "check", "kind": "validate"}]},
        )
    )

    assert response.status_code == 201
    assert json.loads(response.json()["plan_json"])[0]["kind"] == "validate"


def test_create_run_rejects_unknown_step_tool(
    asgi_client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    task = asyncio.run(_create_task(asgi_client, auth_headers))
    response = asyncio.run(
        asgi_client.post(
            f"/api/tasks/{task['id']}/runs",
            headers=auth_headers,
            json={
                "harness": "mock",
                "plan": [
                    {
                        "name": "unknown-tool",
                        "kind": "agent",
                        "params": {"tools_allowed": ["network"]},
                    }
                ],
            },
        )
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation"


def test_create_run_requires_local_token(
    asgi_client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    # fix6: the create_run control route (the loopback exploit's second call) is
    # token-gated — an unauthenticated caller is rejected 401 even for a real task.
    task = asyncio.run(_create_task(asgi_client, auth_headers))
    response = asyncio.run(
        asgi_client.post(
            f"/api/tasks/{task['id']}/runs",
            json={
                "harness": "mock",
                "plan": [
                    {
                        "kind": "agent",
                        "action_class": "sandboxed_creation",
                        "params": {"working_dir": "/Users/x/.ssh", "tools_allowed": ["file_write"]},
                    }
                ],
            },
        )
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_run_detail_uses_bounded_run_event_query(
    asgi_client: httpx.AsyncClient, store: FakeStore, auth_headers: dict[str, str]
) -> None:
    task = asyncio.run(_create_task(asgi_client, auth_headers))
    created = asyncio.run(
        asgi_client.post(
            f"/api/tasks/{task['id']}/runs", headers=auth_headers, json={"harness": "mock"}
        )
    )
    run_id = created.json()["id"]
    calls: list[tuple[str, int]] = []
    original = store.get_events_for_run

    def bounded_events(candidate: str, limit: int = 500) -> list[dict[str, object]]:
        calls.append((candidate, limit))
        return original(candidate, limit)

    def reject_full_scan(*args: object, **kwargs: object) -> list[dict[str, object]]:
        del args, kwargs
        raise AssertionError("run detail must not use the all-events query")

    store.get_events_for_run = bounded_events  # type: ignore[method-assign]
    store.get_events_after = reject_full_scan  # type: ignore[method-assign]
    response = asyncio.run(asgi_client.get(f"/api/runs/{run_id}"))

    assert response.status_code == 200
    assert calls == [(run_id, 500)]
    assert all(event["target_id"] == run_id for event in response.json()["events"])
