"""API route tests for /api/collab using FakeCollabStore."""

from __future__ import annotations

import asyncio

import httpx

from tests.lab.collab.conftest import FakeCollabStore


def test_agents_create_and_list(collab_client: httpx.AsyncClient) -> None:
    created = asyncio.run(
        collab_client.post(
            "/api/collab/agents",
            json={
                "name": "api-agent",
                "lineage": "mock",
                "expertise": ["python"],
                "trust_level": "T2",
            },
        )
    )
    assert created.status_code == 201
    agent = created.json()
    assert agent["name"] == "api-agent"
    assert agent["expertise"] == ["python"]

    listed = asyncio.run(collab_client.get("/api/collab/agents"))
    assert listed.status_code == 200
    assert any(a["id"] == agent["id"] for a in listed.json())


def test_board_create_claim_patch(
    collab_client: httpx.AsyncClient, fake_collab_store: FakeCollabStore
) -> None:
    agent = asyncio.run(
        collab_client.post(
            "/api/collab/agents",
            json={"name": "claimer", "expertise": ["coding"]},
        )
    ).json()

    task_resp = asyncio.run(
        collab_client.post(
            "/api/collab/board",
            json={
                "title": "API task",
                "description": "do work",
                "required_expertise": ["coding"],
                "priority": "high",
            },
        )
    )
    assert task_resp.status_code == 201
    task = task_resp.json()
    assert task["status"] == "open"
    assert task["required_expertise"] == ["coding"]

    # GET /api/collab/board is gone (duplicate of GET /api/board, which needs a
    # real store this double cannot provide), so the created card is verified
    # against the store the route wrote to — the same assertion, one hop closer.
    assert any(
        t["id"] == task["id"] for t in fake_collab_store.list_board_tasks(status="open")
    )

    claim = asyncio.run(
        collab_client.post(
            f"/api/collab/board/{task['id']}/claim",
            json={"agent_id": agent["id"]},
        )
    )
    assert claim.status_code == 200
    assert claim.json() == {
        "success": True,
        "owner_employee_id": None,
        "claimed_by": agent["id"],
    }

    # Second claim races → 409
    claim2 = asyncio.run(
        collab_client.post(
            f"/api/collab/board/{task['id']}/claim",
            json={"agent_id": agent["id"]},
        )
    )
    assert claim2.status_code == 409

    patched = asyncio.run(
        collab_client.patch(
            f"/api/collab/board/{task['id']}",
            json={"status": "done", "result_ref": "run_xyz"},
        )
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "done"
    assert patched.json()["result_ref"] == "run_xyz"


def test_claim_missing_task_404(collab_client: httpx.AsyncClient) -> None:
    resp = asyncio.run(
        collab_client.post(
            "/api/collab/board/btk_missing/claim",
            json={"agent_id": "agt_x"},
        )
    )
    assert resp.status_code == 404


def test_board_restore_route_round_trip(
    collab_client: httpx.AsyncClient, fake_collab_store: FakeCollabStore
) -> None:
    """M-09: restore is reachable and idempotent.

    Archiving now happens ONLY through ``POST /api/board/{id}/archive`` (it
    pauses the card's linked run/session first); the collab archive twin was
    removed. This double has no run lane, so the card is stamped archived
    directly and the restore ROUTE is what is under test here.
    """
    created = asyncio.run(
        collab_client.post(
            "/api/collab/board",
            json={"title": "Restore me", "description": "archive then restore"},
        )
    )
    assert created.status_code == 201
    task = created.json()
    task_id = task["id"]

    fake_collab_store.update_board_task(task_id, {"archived_at": "2026-08-04T00:00:00Z"})
    assert (fake_collab_store.get_board_task(task_id) or {}).get("archived_at") is not None

    restored = asyncio.run(collab_client.post(f"/api/collab/board/{task_id}/restore"))
    assert restored.status_code == 200
    body = restored.json()
    assert body["id"] == task_id
    assert body.get("archived_at") is None

    # Idempotent second restore still 200 with live card.
    again = asyncio.run(collab_client.post(f"/api/collab/board/{task_id}/restore"))
    assert again.status_code == 200
    assert again.json().get("archived_at") is None

    missing = asyncio.run(collab_client.post("/api/collab/board/btk_missing/restore"))
    assert missing.status_code == 404


def test_channels_and_messages(collab_client: httpx.AsyncClient) -> None:
    a1 = asyncio.run(collab_client.post("/api/collab/agents", json={"name": "c1"})).json()
    a2 = asyncio.run(collab_client.post("/api/collab/agents", json={"name": "c2"})).json()
    a3 = asyncio.run(collab_client.post("/api/collab/agents", json={"name": "c3"})).json()

    ch = asyncio.run(
        collab_client.post(
            "/api/collab/channels",
            json={
                "name": "squad",
                "kind": "group",
                "topic": "coord",
                "members": [a1["id"], a2["id"], a3["id"]],
            },
        )
    )
    assert ch.status_code == 201
    channel = ch.json()
    assert set(channel["members"]) == {a1["id"], a2["id"], a3["id"]}

    # DIRECT dedupe
    dm1 = asyncio.run(
        collab_client.post(
            "/api/collab/channels",
            json={
                "name": "dm",
                "kind": "direct",
                "members": [a1["id"], a2["id"]],
            },
        )
    ).json()
    dm2 = asyncio.run(
        collab_client.post(
            "/api/collab/channels",
            json={
                "name": "dm2",
                "kind": "direct",
                "members": [a2["id"], a1["id"]],
            },
        )
    ).json()
    assert dm1["id"] == dm2["id"]

    msg = asyncio.run(
        collab_client.post(
            f"/api/collab/channels/{channel['id']}/messages",
            json={
                "from_agent": a1["id"],
                "body": "hello collab squad",
                "kind": "message",
                "ref": "btk_ref",
            },
        )
    )
    assert msg.status_code == 201
    assert msg.json()["body"] == "hello collab squad"
    assert msg.json()["ref"] == "btk_ref"

    listed = asyncio.run(collab_client.get(f"/api/collab/channels/{channel['id']}/messages"))
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    search = asyncio.run(collab_client.get("/api/collab/messages/search", params={"q": "collab"}))
    assert search.status_code == 200
    assert len(search.json()) == 1

    by_agent = asyncio.run(collab_client.get("/api/collab/channels", params={"agent": a3["id"]}))
    assert by_agent.status_code == 200
    assert any(c["id"] == channel["id"] for c in by_agent.json())


def test_create_agent_validation(collab_client: httpx.AsyncClient) -> None:
    resp = asyncio.run(collab_client.post("/api/collab/agents", json={"name": "  "}))
    assert resp.status_code == 400
