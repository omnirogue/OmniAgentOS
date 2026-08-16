"""D10/F1: the Mode dial is REAL on the DispatchDialog plan-job path.

POST /api/intake/plan persists execute+speed on the job; the background
planning pass honors an EXPLICIT speed (and only an explicit one — a legacy
payload keeps the complexity-derived default planner seam byte-identically);
POST .../confirm threads the persisted (or overridden) dial into
``provision_plan`` with the speed→priority/pins mapping applied at the API
edge, including the ``execute="single"`` hard-suppress passthrough.
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
from omniagentos.intake.fallback import FAST_PLANNER_MODEL
from omniagentos.intake.planner import (
    PlannedTask,
    ProjectPlan,
    ProvisionResult,
    RouteDecision,
)


class _StubHierarchy:
    def create_project(
        self, name: str, *, parent_project_id: str | None = None, description: str = ""
    ) -> str:
        return "prj_stub"

    def list_projects(self) -> list[dict[str, Any]]:
        return []


@pytest.fixture
def collab() -> CollabStore:
    return CollabStore(":memory:")


@pytest.fixture
def client(collab: CollabStore) -> httpx.AsyncClient:
    app.dependency_overrides[get_store] = lambda: collab._store
    app.dependency_overrides[get_collab_store] = lambda: collab
    app.dependency_overrides[intake_routes.get_hierarchy_dal] = _StubHierarchy
    test_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def planned() -> ProjectPlan:
    return ProjectPlan(
        project_name="Dial job",
        description="Make the dial real on the plan path.",
        tasks=[PlannedTask(title="Wire it", acceptance_criteria=["it works"])],
    )


_ROUTE = RouteDecision(decision="new", project_name="Dial job", reason="fresh")


def _wire(
    monkeypatch: pytest.MonkeyPatch, planned: ProjectPlan
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Capture the planning llm + every provision_plan call."""
    plan_calls: list[dict[str, Any]] = []
    provision_calls: list[dict[str, Any]] = []

    def fake_plan(goal: str, **kwargs: Any) -> ProjectPlan:
        plan_calls.append({"goal": goal, **kwargs})
        return planned

    def fake_provision(*_args: Any, **kwargs: Any) -> ProvisionResult:
        provision_calls.append(kwargs)
        return ProvisionResult(root_project_id="prj_stub", route=_ROUTE)

    monkeypatch.setattr(intake_routes, "plan_goal", fake_plan)
    monkeypatch.setattr(intake_routes, "route_project", lambda *_a, **_kw: _ROUTE)
    monkeypatch.setattr(intake_routes, "provision_plan", fake_provision)
    return plan_calls, provision_calls


def _start_and_confirm(
    client: httpx.AsyncClient,
    start_payload: dict[str, Any],
    confirm_payload: dict[str, Any],
) -> None:
    started = asyncio.run(client.post("/api/intake/plan", json=start_payload))
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    # BackgroundTasks ran with the request under ASGITransport — the job is ready.
    status = asyncio.run(client.get(f"/api/intake/plan/{job_id}"))
    assert status.json()["status"] == "ready"
    confirmed = asyncio.run(client.post(f"/api/intake/plan/{job_id}/confirm", json=confirm_payload))
    assert confirmed.status_code == 201


def test_plan_job_persists_dial_and_confirm_threads_it(
    client: httpx.AsyncClient, planned: ProjectPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """execute='single' + speed='fast' persist on the job; the planning pass
    runs the speed seam; confirm maps speed→priority/pins at the API edge."""
    plan_calls, provision_calls = _wire(monkeypatch, planned)

    _start_and_confirm(client, {"goal": "Ship it", "execute": "single", "speed": "fast"}, {})

    # Planning honored the explicit dial speed (not the default seam).
    assert plan_calls[0]["llm"] is not intake_routes.default_planner_llm
    call = provision_calls[0]
    assert call["execute"] == "single"
    assert call["speed"] == "fast"
    assert call["priority"] == "fast"
    assert call["pins"] == {"planner_model": FAST_PLANNER_MODEL}


def test_plan_confirm_explicit_speed_overrides_the_job(
    client: httpx.AsyncClient, planned: ProjectPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, provision_calls = _wire(monkeypatch, planned)

    _start_and_confirm(client, {"goal": "Ship it", "speed": "fast"}, {"speed": "ultra"})

    call = provision_calls[0]
    assert call["speed"] == "ultra"
    assert call["priority"] == "quality"
    assert call["pins"] is None


def test_plan_job_legacy_payload_stays_byte_identical(
    client: httpx.AsyncClient, planned: ProjectPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 regression: a legacy payload (no execute beyond the old vocab, no
    speed) plans on the default seam and provisions with no dial threading."""
    plan_calls, provision_calls = _wire(monkeypatch, planned)

    _start_and_confirm(client, {"goal": "Ship it"}, {})

    assert plan_calls[0]["llm"] is intake_routes.default_planner_llm
    call = provision_calls[0]
    assert call["execute"] == "readonly"
    assert call["speed"] is None
    assert call["priority"] is None
    assert call["pins"] is None


def test_plan_job_explicit_auto_uses_the_speed_seam(
    client: httpx.AsyncClient, planned: ProjectPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: the dial's EXPLICIT 'auto' plans at Fable xhigh (speed seam), and
    confirm maps it to priority='balanced'."""
    plan_calls, provision_calls = _wire(monkeypatch, planned)

    _start_and_confirm(client, {"goal": "Ship it", "speed": "auto"}, {})

    assert plan_calls[0]["llm"] is not intake_routes.default_planner_llm
    call = provision_calls[0]
    assert call["speed"] == "auto"
    assert call["priority"] == "balanced"
    assert call["pins"] is None
