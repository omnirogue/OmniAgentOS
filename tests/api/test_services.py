from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from omniagentos.api.services import ApiError, create_run_service, create_task_service
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.policy import load_policy
from tests.api.fake_store import FakeStore
from tests.support.db_template import make_store


def test_create_services_happy_path_and_prompt_routing(store: FakeStore) -> None:
    policy = load_policy()
    task = create_task_service(
        store,
        policy,
        title="Build briefing",
        discipline_id="research-briefs",
        input={"date": "today"},
        acceptance={"format": "markdown"},
        tools_allowed=["shell"],
    )
    assert task["state"] == "ready"
    assert json.loads(task["input_json"])["tools_allowed"] == ["shell"]
    run = create_run_service(
        store,
        policy,
        task_id=task["id"],
        harness="mock",
        prompt="write the brief",
    )
    assert run["state"] == "queued"
    assert run["priority"] == 2
    assert json.loads(run["plan_json"])[0]["params"]["prompt"] == "write the brief"
    assert [event["action"] for event in store.events] == [
        "task.created",
        "task.queued",
        "run.queued",
    ]


def test_create_run_service_invalid_state(store: FakeStore) -> None:
    policy = load_policy()
    task = create_task_service(store, policy, title="One run only")
    create_run_service(store, policy, task_id=task["id"], harness="mock")
    with pytest.raises(ApiError) as caught:
        create_run_service(store, policy, task_id=task["id"], harness="mock")
    assert caught.value.status_code == 409
    assert caught.value.code == "invalid_state"


def test_unknown_origin_cannot_self_declare_priority_zero(store: FakeStore) -> None:
    task = create_task_service(store, load_policy(), title="Unknown caller")
    run = create_run_service(
        store,
        load_policy(),
        task_id=task["id"],
        harness="mock",
        priority=0,
        origin="unknown",
    )
    assert run["priority"] == 2


def test_scheduled_origin_derives_background_priority(store: FakeStore) -> None:
    task = create_task_service(store, load_policy(), title="Nightly work")
    run = create_run_service(
        store, load_policy(), task_id=task["id"], harness="mock", origin="scheduled"
    )
    assert run["priority"] == 3


def test_improvement_priority_is_derived_from_durable_provenance(tmp_path: Path) -> None:
    store = make_store(SqliteStore, tmp_path / "improvement-priority.db")
    now = utc_now_iso()
    for improvement_id, origin, kind in (
        ("imp_fix", "realtime", "fix"),
        ("imp_optimization", "weekly", "optimization"),
        ("imp_arch_trusted", "audit", "architecture"),
        ("imp_arch_untrusted", "agent_request", "architecture"),
        ("imp_security_trusted", "human", "docs"),
        ("imp_security_untrusted", "agent_request", "docs"),
    ):
        proposal = (
            '{"security_critical":true}' if "security" in improvement_id else "{}"
        )
        store._write(
            "INSERT INTO improvements "
            "(id, origin, kind, title, proposal_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (improvement_id, origin, kind, improvement_id, proposal, now, now),
        )

    observed: dict[str, int] = {}
    for improvement_id in (
        "imp_fix",
        "imp_optimization",
        "imp_arch_trusted",
        "imp_arch_untrusted",
        "imp_security_trusted",
        "imp_security_untrusted",
    ):
        task = create_task_service(store, load_policy(), title=improvement_id)
        run = create_run_service(
            store,
            load_policy(),
            task_id=task["id"],
            harness="mock",
            priority=0,
            origin="improvement",
            provenance_id=improvement_id,
        )
        observed[improvement_id] = int(run["priority"])

    assert observed == {
        "imp_fix": 1,
        "imp_optimization": 1,
        "imp_arch_trusted": 0,
        "imp_arch_untrusted": 2,
        "imp_security_trusted": 0,
        "imp_security_untrusted": 2,
    }


def test_service_prompt_routing_matches_route(
    asgi_client: httpx.AsyncClient, store: FakeStore, auth_headers: dict[str, str]
) -> None:
    route_task = asyncio.run(
        asgi_client.post("/api/tasks", headers=auth_headers, json={"title": "Route"})
    ).json()
    route_run = asyncio.run(
        asgi_client.post(
            f"/api/tasks/{route_task['id']}/runs",
            headers=auth_headers,
            json={"harness": "mock", "prompt": "same prompt"},
        )
    ).json()
    service_task = create_task_service(store, load_policy(), title="Service")
    service_run = create_run_service(
        store,
        load_policy(),
        task_id=service_task["id"],
        harness="mock",
        prompt="same prompt",
    )
    assert json.loads(service_run["plan_json"]) == json.loads(route_run["plan_json"])


def test_steward_routers_are_mounted() -> None:
    # Every steward router must be mounted under its prefix. As packages replace
    # their p1 stub with real routes the /__stub probe disappears (the rewrite IS
    # the deliverable), so asserting a stub response is wrong. Instead assert each
    # prefix owns at least one path in the OpenAPI schema — an anti-drift net that
    # neither a stub nor a real implementation can evade. (This repo wraps routers
    # in _IncludedRouter, so app.routes is not flat; the OpenAPI schema is.)
    from omniagentos.api.main import app

    paths = set(app.openapi()["paths"])
    for name in ("comms", "goals", "briefings", "alerts", "suggestions", "voice"):
        assert any(p == f"/api/{name}" or p.startswith(f"/api/{name}/") for p in paths), (
            f"/api/{name} router not mounted"
        )
