"""WP10: ``POST /api/intake/quick`` gains an ``execute:"swarm"`` passthrough.

Only the literal ``"swarm"`` is accepted (422 otherwise); explicit lanes
(fast/longhaul) keep absolute priority and are never intercepted; the
passthrough mirrors the fast lane's shape — instant placeholder card, then a
BackgroundTask hands the goal to ``dispatch_spec(execute="swarm")``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes import intake as intake_routes
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.store import CollabStore


@pytest.fixture
def collab() -> CollabStore:
    return CollabStore(":memory:")


@pytest.fixture
def client(collab: CollabStore) -> httpx.AsyncClient:
    app.dependency_overrides[get_store] = lambda: collab._store
    app.dependency_overrides[get_collab_store] = lambda: collab
    test_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def dispatch_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_dispatch(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"args": args, **kwargs})
        return {"board_task": {"id": "board_swarm"}, "run_id": None}

    monkeypatch.setattr(intake_routes, "dispatch_spec", fake_dispatch)
    return calls


def test_quick_execute_swarm_passthrough(
    client: httpx.AsyncClient,
    collab: CollabStore,
    dispatch_calls: list[dict[str, Any]],
) -> None:
    goal = "Refactor billing, refresh the docs, and add a smoke suite"
    response = asyncio.run(
        client.post("/api/intake/quick", json={"goal": goal, "execute": "swarm"})
    )

    assert response.status_code == 201
    body = response.json()
    assert body["lane"] == "swarm"
    assert body["status"] == "queued"
    assert body["run_id"].startswith("orch_")

    # Instant placeholder card exists on the board.
    card = collab.get_board_task(body["board_task_id"])
    assert card is not None

    # The background task dispatched with the swarm execute mode, threading the
    # placeholder + correlation run id so a solo fall-through reuses them.
    assert len(dispatch_calls) == 1
    call = dispatch_calls[0]
    assert call["execute"] == "swarm"
    assert call["board_task_id"] == body["board_task_id"]
    assert call["orchestration_run_id"] == body["run_id"]
    assert call["async_orchestrate"] is True
    spec = call["args"][3]
    assert goal in spec.description


def test_quick_execute_rejects_non_swarm_values(
    client: httpx.AsyncClient, dispatch_calls: list[dict[str, Any]]
) -> None:
    response = asyncio.run(
        client.post("/api/intake/quick", json={"goal": "do things", "execute": "tools"})
    )
    assert response.status_code == 422
    assert dispatch_calls == []


def test_explicit_fast_lane_wins_over_swarm(
    client: httpx.AsyncClient, dispatch_calls: list[dict[str, Any]]
) -> None:
    response = asyncio.run(
        client.post(
            "/api/intake/quick",
            json={
                "goal": "make a folder on my desktop called tiger",
                "lane": "fast",
                "execute": "swarm",
            },
        )
    )

    assert response.status_code == 201
    assert response.json()["lane"] == "fast"
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["execute"] == "session"  # the superfast lane, untouched


def test_explicit_longhaul_lane_wins_over_swarm(
    client: httpx.AsyncClient, dispatch_calls: list[dict[str, Any]]
) -> None:
    response = asyncio.run(
        client.post(
            "/api/intake/quick",
            json={
                "goal": "Migrate the ledger schema end to end",
                "lane": "longhaul",
                "execute": "swarm",
            },
        )
    )

    assert response.status_code == 201
    assert response.json()["lane"] == "longhaul"
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["execute"] == "orchestrate"
    assert dispatch_calls[0]["lane"] == "longhaul"


def test_swarm_passthrough_threads_speed_and_priority(
    client: httpx.AsyncClient, dispatch_calls: list[dict[str, Any]]
) -> None:
    """D10: the swarm passthrough carries the dial's speed + mapped priority so
    the provisioned run's plan_json records the mode."""
    response = asyncio.run(
        client.post(
            "/api/intake/quick",
            json={
                "goal": "Refactor billing, refresh the docs, and add a smoke suite",
                "execute": "swarm",
                "speed": "ultra",
            },
        )
    )

    assert response.status_code == 201
    assert response.json()["lane"] == "swarm"
    assert len(dispatch_calls) == 1
    call = dispatch_calls[0]
    assert call["execute"] == "swarm"
    assert call["speed"] == "ultra"
    assert call["priority"] == "quality"


def test_execute_single_never_takes_the_swarm_branch(
    client: httpx.AsyncClient,
    dispatch_calls: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D10: execute='single' is a valid value (201, not 422) that routes down
    the planned lane with the hard-suppress execute mode, never the swarm
    passthrough."""
    from omniagentos.intake.planner import PlannedTask, ProjectPlan

    monkeypatch.setattr(
        intake_routes,
        "plan_goal",
        lambda *_a, **_kw: ProjectPlan(
            project_name="Billing",
            description="Refactor the billing system.",
            tasks=[PlannedTask(title="Refactor billing")],
        ),
    )
    response = asyncio.run(
        client.post(
            "/api/intake/quick",
            json={
                "goal": "Research and refactor the billing system across services",
                "execute": "single",
            },
        )
    )

    assert response.status_code == 201
    assert response.json()["lane"] == "planned"
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["execute"] == "single"


# ---------------------------------------------------------------------------
# D12 force hatch: a leading "swarm:" in the brief forces the swarm branch
# ---------------------------------------------------------------------------


def test_swarm_prefix_takes_the_swarm_branch_and_strips(
    client: httpx.AsyncClient,
    collab: CollabStore,
    dispatch_calls: list[dict[str, Any]],
) -> None:
    """A 'swarm:' brief routes exactly like execute='swarm', with the prefix
    stripped before the spec (and thus any planner) ever sees the goal."""
    response = asyncio.run(
        client.post(
            "/api/intake/quick",
            json={"goal": "swarm: Refactor billing, refresh docs, add a smoke suite"},
        )
    )

    assert response.status_code == 201
    assert response.json()["lane"] == "swarm"
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["execute"] == "swarm"
    spec = dispatch_calls[0]["args"][3]
    assert "swarm:" not in str(getattr(spec, "title", "")).lower()
    assert "swarm:" not in str(getattr(spec, "description", "")).lower()


def test_goal_prefix_outranks_body_execute(
    client: httpx.AsyncClient,
    collab: CollabStore,
    dispatch_calls: list[dict[str, Any]],
) -> None:
    """Most-explicit wins: a 'swarm:' prefix in the user's own text beats an
    automation-supplied execute='single'."""
    response = asyncio.run(
        client.post(
            "/api/intake/quick",
            json={
                "goal": "swarm: Refactor billing, refresh docs, add a smoke suite",
                "execute": "single",
            },
        )
    )

    assert response.status_code == 201
    assert response.json()["lane"] == "swarm"
    assert dispatch_calls[0]["execute"] == "swarm"
