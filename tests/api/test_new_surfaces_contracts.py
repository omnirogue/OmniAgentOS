"""API contract tests for graph, cbm, orgdims, metacog surfaces."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniagentos.api.main import app
from omniagentos.api.routes import cbm as cbm_routes
from omniagentos.api.routes import graph as graph_routes
from omniagentos.api.routes import metacog as metacog_routes
from omniagentos.api.routes import orgdims as orgdims_routes
from omniagentos.cbm.service import CognitiveBudgetService
from omniagentos.graph_runtime.service import GraphRuntimeService
from omniagentos.metacog.config import clear_metacog_config_cache
from omniagentos.metacog.service import MetacogService
from omniagentos.metacog.store import MetacogStore
from omniagentos.orgdims.service import OrgDimsService


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db = str(tmp_path / "api.db")
    monkeypatch.setenv("OMNIAGENTOS_DB", db)
    monkeypatch.setenv("OMNIAGENTOS_METACOG_ARTIFACTS_ROOT", str(tmp_path / "arts"))
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MODE", raising=False)
    clear_metacog_config_cache()
    graph_routes._SERVICE = GraphRuntimeService(db_path=db)
    cbm_routes._SERVICE = CognitiveBudgetService(database=db)
    org = OrgDimsService(db_path=db)
    org.ensure_seeded()
    orgdims_routes._SERVICE = org
    metacog_routes._SERVICE = MetacogService(store=MetacogStore(db))
    return TestClient(app)


def test_health_contracts(client: TestClient) -> None:
    for path in (
        "/api/graph/health",
        "/api/cbm/health",
        "/api/orgdims/health",
        "/api/metacog/health",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        body = r.json()
        assert body.get("ok") is True or body.get("live") is True


def test_graph_not_found_and_invalid(client: TestClient) -> None:
    r = client.get("/api/graph/runs/grn_missing")
    assert r.status_code == 404
    start = client.post("/api/graph/runs/diamond", json={"title": "x"})
    assert start.status_code == 200
    rid = start.json()["run"]["id"]
    bad = client.post(
        f"/api/graph/runs/{rid}/nodes/fan_a/complete",
        json={"outputs": {}},
    )
    assert bad.status_code == 400


def test_cbm_allocate_escalate_contract_flow(client: TestClient) -> None:
    a = client.post(
        "/api/cbm/allocate",
        json={"task_id": "api1", "stage": "execution", "required_quality": 0.9},
    )
    assert a.status_code == 200
    aid = a.json()["allocation"]["id"]
    e = client.post(
        f"/api/cbm/allocations/{aid}/escalate",
        json={"trigger_code": "gate_failure", "evidence": ["x"]},
    )
    assert e.status_code == 200
    assert e.json()["to_rung"] == 2
    c = client.post(
        f"/api/cbm/allocations/{aid}/contract",
        json={"reason": "gate_passed"},
    )
    assert c.status_code == 200


def test_orgdims_seed_and_matrix(client: TestClient) -> None:
    s = client.post("/api/orgdims/seed")
    assert s.status_code == 200
    m = client.get("/api/orgdims/views/matrix")
    assert m.status_code == 200
    p = client.get("/api/orgdims/views/portfolio")
    assert p.status_code == 200
    g = client.get("/api/orgdims/agents/grok")
    assert g.status_code == 200
    assert g.json().get("primary_orchestrator") == "grok-orchestrator" or "agents" in g.json()


def test_metacog_artifact_and_evaluate(client: TestClient) -> None:
    art = client.post(
        "/api/metacog/artifacts/register",
        json={
            "artifact_type": "code_diff",
            "content": '{"diff":"+1"}',
            "task_id": "t1",
        },
    )
    assert art.status_code in {200, 201}, art.text
    ev = client.post(
        "/api/metacog/metacognition/evaluate",
        json={
            "task_id": "t1",
            "criteria_total": 2,
            "criteria_passed": 1,
            "previous_progress": 0.0,
        },
    )
    assert ev.status_code == 200, ev.text
