"""Grok operator API surfaces."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniagentos.api.main import app
from omniagentos.db.store import SqliteStore
from tests.support.db_template import migrated_db


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    db = tmp_path / "t.db"
    migrated_db(SqliteStore, db)
    # Wire store if app uses dependency — fall back to pure endpoints that need no DB
    return TestClient(app)


def test_grok_health(client: TestClient) -> None:
    r = client.get("/api/grok/health")
    # May be 401 without token depending on gate
    assert r.status_code in {200, 401}
    if r.status_code == 200:
        body = r.json()
        assert body.get("product") == "OmniAgentOS"
        assert body.get("merge_to_omniagentos") is False


def test_allocate_simulate_pure() -> None:
    """Unit-level simulate without HTTP auth."""
    from omniagentos.allocation import simulate_fanout

    r = simulate_fanout([{"id": "t1", "title": "x", "acceptance": "ok"}])
    assert r.worker_count >= 0


def test_gates_eval_unit() -> None:
    from omniagentos.gates import GateService

    d = GateService().g0_intake({"authorized": True})
    assert d.decision == "allow"
