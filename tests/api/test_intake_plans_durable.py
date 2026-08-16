"""Plan durability THROUGH THE ROUTES, across a simulated API restart.

The restart is simulated the way it actually happens to the product: the
process-local ``_PLAN_JOBS`` cache is emptied, and every subsequent call has to
come off the durable row or not at all. Both halves are tested, because only one
of them was ever broken in a way anybody noticed:

* the operator can still LOOK at the plan (``GET /api/intake/plan/{job_id}``);
* the operator can still APPROVE it (``POST .../confirm``), which needs the
  route decision and the dial on the row, not just the plan text;
* the approval is written down — status, approver, timestamp, and the execution
  parameters it bound — and is readable back through the route.

Planning is stubbed at the ``plan_goal``/``route_project`` seams (the repo's
existing pattern, see tests/intake/test_plan_job_endpoint.py) so the suite never
depends on a planner CLI. Everything after that — provisioning, preflight, the
durable writes — is the real path.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes import intake as intake_routes
from omniagentos.db.store import SqliteStore
from omniagentos.intake.planner import PlannedTask, ProjectPlan, RouteDecision
from omniagentos.plans.store import PlansStore

GOAL = "Ship the durable plans spine"
TASK_TITLE = "Wire omniagentos/plans/ into the intake route"


@pytest.fixture
def project_name() -> str:
    """Unique per test: provisioning refuses a duplicate project name, and the
    whole suite shares one control-plane store."""
    return f"Durable plans {uuid.uuid4().hex[:8]}"


@pytest.fixture
def plans_store() -> PlansStore:
    """The DAL over the very store the app serves requests from."""
    return PlansStore(cast(SqliteStore, get_store()))


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, project_name: str) -> TestClient:
    """A client whose planning is deterministic; everything else is production."""
    planned = ProjectPlan(
        project_name=project_name,
        description="Plans survive a restart, and so does the approval.",
        tasks=[
            PlannedTask(
                title=TASK_TITLE,
                description="Persist the plan job and bind the approval.",
                acceptance_criteria=["the plan is readable after a restart"],
            )
        ],
    )
    route = RouteDecision(decision="new", project_name=project_name, reason="fresh project")
    monkeypatch.setattr(intake_routes, "plan_goal", lambda goal, **_kw: planned)
    monkeypatch.setattr(intake_routes, "route_project", lambda *_a, **_kw: route)
    return TestClient(app)


def _start_ready_plan(client: TestClient, **overrides: Any) -> str:
    """POST /intake/plan and return the job id, already ``ready``.

    ``BackgroundTasks`` run inside the ASGI call, so the planning pass (and its
    durable write) has already happened when this returns.
    """
    payload: dict[str, Any] = {"goal": GOAL, "execute": "readonly", "speed": "fast"}
    payload.update(overrides)
    started = client.post("/api/intake/plan", json=payload)
    assert started.status_code == 202, started.text
    job_id = str(started.json()["job_id"])
    live = client.get(f"/api/intake/plan/{job_id}")
    assert live.status_code == 200, live.text
    assert live.json()["status"] == "ready", live.text
    return job_id


def _restart() -> None:
    """Simulate an API restart: the process-local job cache is gone."""
    with intake_routes._PLAN_JOBS_LOCK:
        intake_routes._PLAN_JOBS.clear()


def test_plan_survives_restart_through_the_route(
    client: TestClient, plans_store: PlansStore, project_name: str
) -> None:
    """After a restart the poll route still serves the plan AND its route decision."""
    job_id = _start_ready_plan(client)
    assert client.get(f"/api/intake/plan/{job_id}").json()["durable"] is True

    _restart()

    polled = client.get(f"/api/intake/plan/{job_id}")
    assert polled.status_code == 200, (
        f"plan {job_id} was LOST after restart: the poll route 404s, so the "
        f"durable row was never written ({polled.text})"
    )
    body = polled.json()
    assert body["status"] == "ready", f"plan {job_id} was LOST after restart: {body}"
    assert body["plan"]["project_name"] == project_name
    assert [task["title"] for task in body["plan"]["tasks"]] == [TASK_TITLE]
    # The route decision is the half that a plan-only row loses; without it the
    # confirm below cannot run at all.
    assert body["route"] is not None, f"route was LOST after restart for {job_id}"
    assert body["route"]["decision"] == "new"
    assert body["durable"] is True

    row = plans_store.get_plan(job_id)
    assert row is not None
    assert row["status"] == "ready"
    assert row["goal"] == GOAL
    assert row["execute_mode"] == "readonly"
    assert row["speed"] == "fast"
    assert json.loads(row["route_json"])["decision"] == "new"


def test_confirm_after_restart_binds_the_approval(
    client: TestClient, plans_store: PlansStore
) -> None:
    """The decisive one: approve a plan whose in-memory job is gone, and record it.

    Looking at a plan you cannot approve is not durability, and a 201 that left
    ``status='ready', decided_by=NULL, execution_metadata=NULL`` behind is not an
    approval.
    """
    job_id = _start_ready_plan(client)
    _restart()

    confirmed = client.post(f"/api/intake/plan/{job_id}/confirm", json={"project_override": "auto"})
    assert confirmed.status_code == 201, (
        f"plan {job_id} could not be APPROVED after restart: {confirmed.status_code} "
        f"{confirmed.text}"
    )
    payload = confirmed.json()
    assert payload["task_count"] >= 1
    assert payload["durable_approval"] is True, (
        "approval was NOT bound: the route reported success while the durable write failed"
    )

    row = plans_store.get_plan(job_id)
    assert row is not None
    assert row["status"] == "approved", (
        f"approval was NOT bound to the durable plan: status={row['status']!r}, "
        f"decided_by={row['decided_by']!r}, execution_metadata={row['execution_metadata']!r}"
    )
    assert row["decided_by"] == "operator"
    assert row["decided_at"]
    assert row["execution_metadata"], (
        "approval was NOT bound: execution_metadata is empty, so nothing records "
        "what the operator approved"
    )

    execution = json.loads(row["execution_metadata"])
    assert execution["task_count"] == payload["task_count"]
    assert execution["execute"] == "readonly"
    assert execution["root_project_id"] == payload["root_project_id"]
    # One entry per dispatched card, written ONCE for the whole plan.
    assert len(execution["cards"]) == len(payload["dispatched"])
    # The BOARD CARD ids, not the control-plane task ids dispatch_spec also
    # returns: plans.board_task_id has a foreign key into board_tasks.
    dispatched_cards = {str(card["board_task"]["id"]) for card in payload["dispatched"]}
    assert {str(card["board_task_id"]) for card in execution["cards"]} == dispatched_cards
    assert all(card["formation_id"] for card in execution["cards"])
    # A single-card approval binds the card on the row; a multi-card one leaves
    # it null rather than claiming a primary card that does not exist.
    if len(execution["cards"]) == 1:
        assert row["board_task_id"] == execution["cards"][0]["board_task_id"]
    else:
        assert row["board_task_id"] is None


def test_approved_execution_is_readable_through_the_route(client: TestClient) -> None:
    """``execution_metadata`` has a reader: the poll route serves it back."""
    job_id = _start_ready_plan(client)
    confirmed = client.post(f"/api/intake/plan/{job_id}/confirm", json={})
    assert confirmed.status_code == 201, confirmed.text

    _restart()

    polled = client.get(f"/api/intake/plan/{job_id}")
    assert polled.status_code == 200, polled.text
    body = polled.json()
    assert body["status"] == "confirmed"
    assert body["execution"] is not None, (
        "approval was NOT bound: nothing reads execution_metadata back, so the column is write-only"
    )
    assert body["execution"]["cards"], body["execution"]
    assert body["execution"]["owned_paths"] == sorted(body["execution"]["owned_paths"])
    assert body["durable"] is True


def test_confirm_after_restart_never_provisions_twice(
    client: TestClient, plans_store: PlansStore
) -> None:
    """An already-approved plan is refused, not dispatched a second time.

    The cached provision result does not survive a restart, so idempotency has
    to come off the durable row — otherwise a client retry after a reboot
    creates the project and every card again.
    """
    job_id = _start_ready_plan(client)
    first = client.post(f"/api/intake/plan/{job_id}/confirm", json={})
    assert first.status_code == 201, first.text
    approved_at = plans_store.get_plan(job_id)["decided_at"]

    _restart()

    again = client.post(f"/api/intake/plan/{job_id}/confirm", json={})
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "already_confirmed"

    row = plans_store.get_plan(job_id)
    assert row is not None
    assert row["status"] == "approved"
    assert row["decided_at"] == approved_at


def test_confirm_records_the_authenticated_approver(
    client: TestClient, plans_store: PlansStore
) -> None:
    """``decided_by`` records the principal, and a caller cannot forge it.

    The control plane has one principal; an approval attributed to whatever
    name the request body carried would be an audit field that proves nothing.
    """
    job_id = _start_ready_plan(client)
    confirmed = client.post(
        f"/api/intake/plan/{job_id}/confirm", json={"decided_by": "somebody-else"}
    )
    assert confirmed.status_code == 201, confirmed.text
    row = plans_store.get_plan(job_id)
    assert row is not None
    assert row["decided_by"] == "operator"


def test_unknown_plan_job_is_still_a_404(client: TestClient) -> None:
    """The durable fallback must not turn every miss into a hang or a 500."""
    assert client.get("/api/intake/plan/planjob_does_not_exist").status_code == 404
    assert (
        client.post("/api/intake/plan/planjob_does_not_exist/confirm", json={}).status_code == 404
    )
