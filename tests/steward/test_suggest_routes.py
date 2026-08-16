from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import omniagentos.api.routes.suggestions as suggestions_routes
from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.contracts import ActionClass, Events, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.policy import evaluate_action, load_policy
from omniagentos.steward.store import StewardStore


@pytest.fixture
def sqlite_store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(str(tmp_path / "suggest_routes.db"))


@pytest.fixture
def steward(sqlite_store: SqliteStore) -> StewardStore:
    return StewardStore(sqlite_store)


@pytest.fixture
def asgi_client(sqlite_store: SqliteStore) -> Generator[httpx.AsyncClient, None, None]:
    app.dependency_overrides[get_store] = lambda: sqlite_store
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()
        suggestions_routes.get_steward_config.cache_clear()


def _seed_goal_with_discipline(sqlite_store: SqliteStore, steward: StewardStore) -> dict[str, Any]:
    sqlite_store.create_discipline(
        {
            "id": "revenue",
            "name": "Revenue",
            "metric_contract": "{}",
            "status": "active",
            "created_at": utc_now_iso(),
        }
    )
    return steward.upsert_goal(
        {
            "name": "increase-revenue",
            "discipline_id": "revenue",
            "north_star": {"source": "stripe", "metric": "net_revenue_usd"},
            "status": "active",
        }
    )


def _create_suggestion(
    steward: StewardStore,
    goal_id: str | None,
    *,
    title: str = "Investigate revenue dip",
    action_class: str = "read_only",
    prompt: str = "analyze recent Stripe revenue data read-only",
) -> dict[str, Any]:
    return steward.create_suggestion(
        {
            "title": title,
            "rationale": "test rationale",
            "evidence": [{"metric": "net_revenue_usd"}],
            "proposed_plan": {
                "prompt": prompt,
                "harness": "cli-claude",
                "action_class": action_class,
            },
            "risk_class": "read_only",
            "source": "steward.suggest.revenue_downtrend",
            "goal_id": goal_id,
        }
    )


def _events(sqlite_store: SqliteStore, suggestion_id: str) -> list[dict[str, Any]]:
    rows = sqlite_store.get_events_after(0, types=[Events.SUGGESTION_UPDATED], limit=500)
    return [row for row in rows if row.get("target_id") == suggestion_id]


def test_list_suggestions_filters_by_state(
    asgi_client: httpx.AsyncClient, sqlite_store: SqliteStore, steward: StewardStore
) -> None:
    goal = _seed_goal_with_discipline(sqlite_store, steward)
    open_row = _create_suggestion(steward, goal["id"])
    dismissed_row = _create_suggestion(
        steward, goal["id"], title="Consider scaling winning campaigns"
    )
    steward.decide_suggestion(str(dismissed_row["id"]), state="dismissed", decided_by="owner")

    open_resp = asyncio.run(asgi_client.get("/api/suggestions", params={"state": "open"}))
    all_resp = asyncio.run(asgi_client.get("/api/suggestions"))

    assert open_resp.status_code == 200
    assert [row["id"] for row in open_resp.json()] == [open_row["id"]]
    assert {row["id"] for row in all_resp.json()} == {open_row["id"], dismissed_row["id"]}


def test_generate_creates_suggestions_from_seeded_metrics(
    asgi_client: httpx.AsyncClient, sqlite_store: SqliteStore, steward: StewardStore
) -> None:
    from datetime import UTC, datetime, timedelta

    goal = _seed_goal_with_discipline(sqlite_store, steward)
    goal_id = str(goal["id"])
    values = [100.0, 100.0, 100.0, 100.0, 70.0, 70.0, 70.0]
    for offset, value in enumerate(values):
        steward.insert_metric_snapshot(
            {
                "goal_id": goal_id,
                "source": "stripe",
                "metric": "net_revenue_usd",
                "value": value,
                "captured_at": (datetime.now(UTC) - timedelta(days=len(values) - offset)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
        )

    resp = asyncio.run(asgi_client.post("/api/suggestions/generate"))

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["title"] == "Investigate revenue dip"
    assert len(_events(sqlite_store, str(body[0]["id"]))) == 1


def test_approve_happy_path_creates_task_and_run_and_emits_events(
    asgi_client: httpx.AsyncClient, sqlite_store: SqliteStore, steward: StewardStore
) -> None:
    goal = _seed_goal_with_discipline(sqlite_store, steward)
    suggestion = _create_suggestion(steward, goal["id"])

    resp = asyncio.run(
        asgi_client.post(
            f"/api/suggestions/{suggestion['id']}/approve", json={"approved_by": "owner"}
        )
    )

    assert resp.status_code == 200
    body = resp.json()
    task_id = body["task_id"]
    run_id = body["run_id"]

    # (a) task+run rows exist with correct plan/prompt
    task = sqlite_store.get_task(task_id)
    assert task is not None
    assert task["state"] == "queued"
    assert task["discipline_id"] == "revenue"
    assert json.loads(task["input_json"])["suggestion_id"] == suggestion["id"]

    run = sqlite_store.get_run(run_id)
    assert run is not None
    plan = json.loads(run["plan_json"])
    assert plan == [
        {
            "name": "agent",
            "kind": "agent",
            "action_class": "read_only",
            "params": {"prompt": "analyze recent Stripe revenue data read-only"},
        }
    ]

    # (b) run visible via GET /api/runs
    runs_resp = asyncio.run(asgi_client.get("/api/runs"))
    assert any(row["id"] == run_id for row in runs_resp.json())

    # (c) suggestion decided with ids
    assert body["suggestion"]["state"] == "approved"
    assert body["suggestion"]["task_id"] == task_id
    assert body["suggestion"]["run_id"] == run_id
    assert body["suggestion"]["decided_by"] == "owner"
    decided = steward.get_suggestion(suggestion["id"])
    assert decided is not None
    assert decided["state"] == "approved"

    # (d) events emitted
    events = _events(sqlite_store, str(suggestion["id"]))
    actions = [row["action"] for row in events]
    assert "suggestion.approved" in actions


def test_concurrent_approve_creates_exactly_one_run(
    sqlite_store: SqliteStore, steward: StewardStore
) -> None:
    # H9: two operators approve the SAME suggestion simultaneously. The
    # claim-first gate must let exactly one win: exactly one task+run is
    # created, the loser gets 409, and the suggestion ends 'approved' with a
    # single run_id. Without claim-first both callers pass the state=='open'
    # check and both mint real runs.
    goal = _seed_goal_with_discipline(sqlite_store, steward)
    suggestion = _create_suggestion(steward, goal["id"])

    app.dependency_overrides[get_store] = lambda: sqlite_store
    statuses: list[int] = []
    bodies: list[dict[str, Any]] = []
    lock = threading.Lock()
    try:
        with TestClient(app) as client:
            barrier = threading.Barrier(2)

            def _approve() -> None:
                barrier.wait()
                resp = client.post(
                    f"/api/suggestions/{suggestion['id']}/approve",
                    json={"approved_by": "owner"},
                )
                with lock:
                    statuses.append(resp.status_code)
                    bodies.append(resp.json())

            threads = [threading.Thread(target=_approve) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
    finally:
        app.dependency_overrides.clear()
        suggestions_routes.get_steward_config.cache_clear()

    # Exactly one winner (200) and one loser (409, invalid_state).
    assert sorted(statuses) == [200, 409]
    loser = next(body for status, body in zip(statuses, bodies, strict=True) if status == 409)
    assert loser["error"]["code"] == "invalid_state"

    # Exactly one run exists in the store.
    runs = sqlite_store.list_runs({}, limit=500)
    assert len(runs) == 1

    decided = steward.get_suggestion(suggestion["id"])
    assert decided is not None
    assert decided["state"] == "approved"
    assert decided["run_id"] == runs[0]["id"]

    winner = next(body for status, body in zip(statuses, bodies, strict=True) if status == 200)
    assert winner["run_id"] == runs[0]["id"]


def test_approve_passes_accurate_risk_and_exposes_reconciled_card(
    asgi_client: httpx.AsyncClient, sqlite_store: SqliteStore, steward: StewardStore
) -> None:
    # H13: a consequential suggestion (even one whose stored risk_class was the
    # under-stated 'read_only' the create helper writes) must produce a Task
    # labeled risk 'high', a run step of action_class 'consequential', and a
    # response card whose risk_class/proposed_plan reflect the truth.
    goal = _seed_goal_with_discipline(sqlite_store, steward)
    suggestion = _create_suggestion(
        steward,
        goal["id"],
        title="Launch a paid campaign",
        action_class="consequential",
        prompt="launch the ad campaign",
    )
    # Confirm the stored card under-states the risk before approval.
    stored = steward.get_suggestion(suggestion["id"])
    assert stored is not None and stored["risk_class"] == "read_only"

    resp = asyncio.run(
        asgi_client.post(
            f"/api/suggestions/{suggestion['id']}/approve", json={"approved_by": "owner"}
        )
    )

    assert resp.status_code == 200
    body = resp.json()

    # (a) Task labeled with the accurate coarse risk, not a hardcoded 'low'.
    task = sqlite_store.get_task(body["task_id"])
    assert task is not None
    assert task["risk"] == "high"

    # (b) The run step still carries the real action_class (broker gate intact).
    run = sqlite_store.get_run(body["run_id"])
    assert run is not None
    assert json.loads(run["plan_json"])[0]["action_class"] == "consequential"

    # (c) Frozen response shape for RF: reconciled risk_class + proposed_plan +
    # evidence all reflect what actually executes.
    card = body["suggestion"]
    assert card["risk_class"] == "consequential"
    assert card["proposed_plan"]["action_class"] == "consequential"
    assert card["proposed_plan"]["prompt"] == "launch the ad campaign"
    assert card["proposed_plan"]["harness"] == "cli-claude"
    assert card["evidence"] == [{"metric": "net_revenue_usd"}]


def test_list_suggestions_exposes_reconciled_risk_class(
    asgi_client: httpx.AsyncClient, sqlite_store: SqliteStore, steward: StewardStore
) -> None:
    # GET /api/suggestions is what the dashboard (RF) renders: risk_class must
    # be reconciled to the effective action_class there too.
    goal = _seed_goal_with_discipline(sqlite_store, steward)
    _create_suggestion(
        steward,
        goal["id"],
        title="Launch a paid campaign",
        action_class="consequential",
    )

    resp = asyncio.run(asgi_client.get("/api/suggestions", params={"state": "open"}))

    assert resp.status_code == 200
    card = resp.json()[0]
    assert card["risk_class"] == "consequential"
    assert card["proposed_plan"]["action_class"] == "consequential"


def test_list_suggestions_exposes_irreversible_risk_class(
    asgi_client: httpx.AsyncClient, sqlite_store: SqliteStore, steward: StewardStore
) -> None:
    # Regression: _ACTION_CLASSES previously omitted "irreversible", so
    # reconcile_suggestion (run on every /api/suggestions response) rewrote a
    # stored irreversible suggestion down to the sandboxed default before the
    # dashboard ever saw it -- reopening the RISK_CLASSES fail-open defect
    # end-to-end even after the frontend fix landed.
    goal = _seed_goal_with_discipline(sqlite_store, steward)
    _create_suggestion(
        steward,
        goal["id"],
        title="Irreversibly delete the archive",
        action_class="irreversible",
    )

    resp = asyncio.run(asgi_client.get("/api/suggestions", params={"state": "open"}))

    assert resp.status_code == 200
    card = resp.json()[0]
    assert card["risk_class"] == "irreversible"
    assert card["proposed_plan"]["action_class"] == "irreversible"


def test_approve_consequential_class_flows_to_plan_and_policy_requires_approval(
    asgi_client: httpx.AsyncClient, sqlite_store: SqliteStore, steward: StewardStore
) -> None:
    goal = _seed_goal_with_discipline(sqlite_store, steward)
    suggestion = _create_suggestion(
        steward,
        goal["id"],
        title="Launch a paid campaign",
        action_class="consequential",
        prompt="launch the ad campaign",
    )

    resp = asyncio.run(
        asgi_client.post(
            f"/api/suggestions/{suggestion['id']}/approve", json={"approved_by": "owner"}
        )
    )

    assert resp.status_code == 200
    run = sqlite_store.get_run(resp.json()["run_id"])
    assert run is not None
    plan = json.loads(run["plan_json"])
    assert plan[0]["action_class"] == "consequential"

    # Policy-config assertion only -- never runs the runner/broker (per brief).
    # Asserted in SUPERVISED mode: it captures the legacy consequential floor. Under
    # the AUTO default consequential auto-executes; a paid campaign is money-movement
    # whose real hard-stop is the broker (payment WRITE) + the irreversible class.
    from omniagentos.policy import PolicyMode

    policy_cfg = load_policy().model_copy(update={"mode": PolicyMode.SUPERVISED})
    decision = evaluate_action(ActionClass.CONSEQUENTIAL, policy_cfg)
    assert decision.requires_approval is True
    assert decision.always_human is True


def test_approve_unknown_suggestion_is_404(asgi_client: httpx.AsyncClient) -> None:
    resp = asyncio.run(
        asgi_client.post("/api/suggestions/sug_missing/approve", json={"approved_by": "owner"})
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_approve_twice_is_409_on_second_call(
    asgi_client: httpx.AsyncClient, sqlite_store: SqliteStore, steward: StewardStore
) -> None:
    goal = _seed_goal_with_discipline(sqlite_store, steward)
    suggestion = _create_suggestion(steward, goal["id"])

    first = asyncio.run(
        asgi_client.post(
            f"/api/suggestions/{suggestion['id']}/approve", json={"approved_by": "owner"}
        )
    )
    second = asyncio.run(
        asgi_client.post(
            f"/api/suggestions/{suggestion['id']}/approve", json={"approved_by": "owner"}
        )
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invalid_state"


def test_approve_missing_approved_by_is_422(
    asgi_client: httpx.AsyncClient, sqlite_store: SqliteStore, steward: StewardStore
) -> None:
    goal = _seed_goal_with_discipline(sqlite_store, steward)
    suggestion = _create_suggestion(steward, goal["id"])

    resp = asyncio.run(asgi_client.post(f"/api/suggestions/{suggestion['id']}/approve", json={}))

    assert resp.status_code == 422


@pytest.mark.parametrize("identity", ["auto", "System", " scheduler ", "AUTOPILOT"])
def test_approve_rejects_reserved_auto_identities(
    asgi_client: httpx.AsyncClient,
    sqlite_store: SqliteStore,
    steward: StewardStore,
    identity: str,
) -> None:
    goal = _seed_goal_with_discipline(sqlite_store, steward)
    suggestion = _create_suggestion(steward, goal["id"])

    resp = asyncio.run(
        asgi_client.post(
            f"/api/suggestions/{suggestion['id']}/approve", json={"approved_by": identity}
        )
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "autonomy"
    # Refused before any task/run is created.
    assert steward.get_suggestion(suggestion["id"])["state"] == "open"


def test_dismiss_records_reason_and_emits_event(
    asgi_client: httpx.AsyncClient, sqlite_store: SqliteStore, steward: StewardStore
) -> None:
    goal = _seed_goal_with_discipline(sqlite_store, steward)
    suggestion = _create_suggestion(steward, goal["id"])

    resp = asyncio.run(
        asgi_client.post(
            f"/api/suggestions/{suggestion['id']}/dismiss",
            json={"decided_by": "owner", "reason": "not a priority"},
        )
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "dismissed"
    assert body["decided_by"] == "owner"
    assert body["outcome"] == {"dismiss_reason": "not a priority"}

    events = _events(sqlite_store, str(suggestion["id"]))
    assert any(row["action"] == "suggestion.dismissed" for row in events)


def test_dismiss_missing_decided_by_is_422(
    asgi_client: httpx.AsyncClient, sqlite_store: SqliteStore, steward: StewardStore
) -> None:
    goal = _seed_goal_with_discipline(sqlite_store, steward)
    suggestion = _create_suggestion(steward, goal["id"])

    resp = asyncio.run(asgi_client.post(f"/api/suggestions/{suggestion['id']}/dismiss", json={}))

    assert resp.status_code == 422


def test_dismiss_twice_is_409_on_second_call(
    asgi_client: httpx.AsyncClient, sqlite_store: SqliteStore, steward: StewardStore
) -> None:
    goal = _seed_goal_with_discipline(sqlite_store, steward)
    suggestion = _create_suggestion(steward, goal["id"])

    first = asyncio.run(
        asgi_client.post(f"/api/suggestions/{suggestion['id']}/dismiss", json={"decided_by": "owner"})
    )
    second = asyncio.run(
        asgi_client.post(f"/api/suggestions/{suggestion['id']}/dismiss", json={"decided_by": "owner"})
    )

    assert first.status_code == 200
    assert second.status_code == 409


def test_approve_surfaces_502_and_leaves_suggestion_open_when_run_creation_fails(
    asgi_client: httpx.AsyncClient,
    sqlite_store: SqliteStore,
    steward: StewardStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omniagentos.api.services import ApiError

    goal = _seed_goal_with_discipline(sqlite_store, steward)
    suggestion = _create_suggestion(steward, goal["id"])

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise ApiError(422, "validation", "forced failure for test")

    monkeypatch.setattr(suggestions_routes, "create_run_service", _boom)

    resp = asyncio.run(
        asgi_client.post(
            f"/api/suggestions/{suggestion['id']}/approve", json={"approved_by": "owner"}
        )
    )

    assert resp.status_code == 502
    error = resp.json()["error"]
    assert error["code"] == "run_creation_failed"
    assert error["detail"]["suggestion_id"] == suggestion["id"]
    assert "task_id" in error["detail"]

    # The suggestion was NOT decided -- it remains open for retry.
    decided = steward.get_suggestion(suggestion["id"])
    assert decided is not None
    assert decided["state"] == "open"

    # The task from create_task_service was created and is left visible (the
    # partial-task caveat from ADR-006), even though the run failed.
    task = sqlite_store.get_task(error["detail"]["task_id"])
    assert task is not None
