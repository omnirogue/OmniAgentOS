"""API smoke for /api/graph and /api/cbm (LIVE product surfaces)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniagentos.api.main import app
from omniagentos.api.routes import cbm as cbm_routes
from omniagentos.api.routes import graph as graph_routes
from omniagentos.cbm.service import CognitiveBudgetService
from omniagentos.graph_runtime.service import GraphRuntimeService


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db = str(tmp_path / "api.db")
    monkeypatch.setenv("OMNIAGENTOS_DB", db)
    # Reset singletons so they bind to temp DB
    graph_routes._SERVICE = GraphRuntimeService(db_path=db)
    cbm_routes._SERVICE = CognitiveBudgetService(database=db)
    return TestClient(app)


def test_graph_health_and_diamond_demo(client: TestClient) -> None:
    r = client.get("/api/graph/health")
    assert r.status_code == 200
    body = r.json()
    assert body["live"] is True
    assert body["version"] == "graph-v2"

    demo = client.post("/api/graph/demo/diamond", json={"title": "api-diamond"})
    assert demo.status_code == 200
    data = demo.json()
    assert data["ok"] is True
    assert data["status"] == "completed"
    run_id = data["run"]["id"]

    view = client.get(f"/api/graph/runs/{run_id}/view")
    assert view.status_code == 200
    assert view.json()["status"] == "completed"


def test_graph_start_and_complete(client: TestClient) -> None:
    start = client.post(
        "/api/graph/runs/diamond",
        json={"title": "stepwise", "completeness_policy": "fail_closed"},
    )
    assert start.status_code == 200
    run = start.json()["run"]
    run_id = run["id"]

    for key, payload in (
        ("fan_a", {"finding": {"claim": "x", "score": 0.7}}),
        ("fan_b", {"finding": {"claim": "y", "score": 0.8}}),
    ):
        c = client.post(
            f"/api/graph/runs/{run_id}/nodes/{key}/complete",
            json={"outputs": payload},
        )
        assert c.status_code == 200

    ready = client.get(f"/api/graph/runs/{run_id}/ready")
    assert ready.status_code == 200
    keys = {n["key"] for n in ready.json()["ready"]}
    assert "reduce" in keys


def test_cbm_allocate_escalate_close(client: TestClient) -> None:
    h = client.get("/api/cbm/health")
    assert h.status_code == 200
    assert h.json()["live"] is True
    assert h.json()["cost_objective"] is False

    rungs = client.get("/api/cbm/rungs")
    assert rungs.status_code == 200
    assert len(rungs.json()["rungs"]) == 7

    alloc = client.post(
        "/api/cbm/allocate",
        json={
            "task_id": "api-task-1",
            "stage": "execution",
            "risk_class": "reversible_internal",
            "novelty": "low",
        },
    )
    assert alloc.status_code == 200
    a = alloc.json()["allocation"]
    assert a["rung"] == 1
    aid = a["id"]

    esc = client.post(
        f"/api/cbm/allocations/{aid}/escalate",
        json={"trigger_code": "gate_failure", "evidence": ["unit failed"]},
    )
    assert esc.status_code == 200
    assert esc.json()["to_rung"] == 2

    close = client.post(
        f"/api/cbm/allocations/{aid}/close",
        json={"first_pass_accepted": False, "wall_seconds": 12.5, "repair_count": 1},
    )
    assert close.status_code == 200
    assert close.json()["closed"] is True

    board = client.get("/api/cbm/leaderboard")
    assert board.status_code == 200
