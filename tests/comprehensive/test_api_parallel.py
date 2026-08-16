"""HTTP-level parallel smoke for graph + CBM + orgdims + metacog APIs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
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
    db = str(tmp_path / "api-par.db")
    monkeypatch.setenv("OMNIAGENTOS_DB", db)
    monkeypatch.setenv("OMNIAGENTOS_METACOG_ARTIFACTS_ROOT", str(tmp_path / "arts"))
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MODE", raising=False)
    clear_metacog_config_cache()
    graph_routes._SERVICE = GraphRuntimeService(db_path=db)
    cbm_routes._SERVICE = CognitiveBudgetService(database=db)
    orgdims_routes._SERVICE = OrgDimsService(db_path=db)
    orgdims_routes._SERVICE.ensure_seeded()
    metacog_routes._SERVICE = MetacogService(store=MetacogStore(db))
    return TestClient(app)


def test_parallel_api_health_storm(client: TestClient, workers: int) -> None:
    paths = [
        "/api/graph/health",
        "/api/cbm/health",
        "/api/orgdims/health",
        "/api/metacog/health",
    ]

    def hit(path: str) -> tuple[str, int, bool]:
        r = client.get(path)
        body = r.json()
        return path, r.status_code, bool(body.get("ok") or body.get("live"))

    n = workers * 2
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(hit, paths[i % len(paths)]) for i in range(n)]
        results = [f.result(timeout=20) for f in as_completed(futs)]

    assert all(code == 200 for _, code, _ in results)
    assert all(ok for _, _, ok in results)


def test_parallel_diamond_demos(client: TestClient, workers: int) -> None:
    n = min(workers, 12)

    def demo(i: int) -> str:
        r = client.post(
            "/api/graph/demo/diamond",
            json={"title": f"api-par-{i}"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] == "completed"
        return data["run"]["id"]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        ids = [f.result(timeout=45) for f in as_completed([pool.submit(demo, i) for i in range(n)])]
    assert len(set(ids)) == n


def test_parallel_cbm_allocate_api(client: TestClient, workers: int) -> None:
    n = min(workers * 2, 32)

    def alloc(i: int) -> int:
        r = client.post(
            "/api/cbm/allocate",
            json={
                "task_id": f"api-t-{i}",
                "stage": "execution",
                "risk_class": "reversible_internal",
            },
        )
        assert r.status_code == 200
        return int(r.json()["allocation"]["rung"])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rungs = [
            f.result(timeout=30) for f in as_completed([pool.submit(alloc, i) for i in range(n)])
        ]
    assert all(r >= 0 for r in rungs)
    assert rungs.count(1) >= n // 2  # most are fast-first
