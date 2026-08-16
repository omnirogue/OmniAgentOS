"""HTTP contract for lifecycle-scoped access grants."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from omniagentos.api.main import app
from omniagentos.api.routes import access
from omniagentos.api.routes import collab as collab_routes
from omniagentos.collab.contracts import Agent
from omniagentos.collab.store import CollabStore
from omniagentos.connectors.store import CapabilityStore


@pytest.fixture
def client(tmp_path) -> Iterator[tuple[TestClient, CapabilityStore, str]]:
    collab = CollabStore(db_path=str(tmp_path / "access.db"))
    agent = Agent(name="route-agent")
    collab.register_agent(agent)
    caps = CapabilityStore(collab._store)
    app.dependency_overrides[access.get_capability_store] = lambda: caps
    app.dependency_overrides[collab_routes.get_collab_store] = lambda: collab
    with TestClient(app) as test_client:
        yield test_client, caps, agent.id
    app.dependency_overrides.clear()


def test_put_agents_with_scoped_grant(client: tuple[TestClient, CapabilityStore, str]) -> None:
    test_client, caps, agent_id = client
    response = test_client.put(
        f"/api/access/agents/{agent_id}",
        json={
            "granted": ["piedpiper_acmeuni.read"],
            "mode": "read",
            "expires_at": "2030-01-02T03:04:05Z",
            "issued_by": "approval:apr_123",
        },
    )
    assert response.status_code == 200
    assert response.json()["granted"] == ["piedpiper_acmeuni.read"]
    row = caps.get_grant_row(agent_id, "piedpiper_acmeuni.read")
    assert row is not None and row["issued_by"] == "approval:apr_123"


def test_put_agents_fallback_to_set_grant(client: tuple[TestClient, CapabilityStore, str]) -> None:
    test_client, caps, agent_id = client
    caps.issue_scoped_grant(agent_id, ["piedpiper_acmeuni.note_write"], mode="write")
    response = test_client.put(f"/api/access/agents/{agent_id}", json={"granted": ["piedpiper_acmeuni.read"]})
    assert response.status_code == 200
    assert response.json()["granted"] == ["piedpiper_acmeuni.read"]
    assert caps.get_grant_row(agent_id, "piedpiper_acmeuni.note_write") is None


def test_put_agents_invalid_mode(client: tuple[TestClient, CapabilityStore, str]) -> None:
    test_client, _caps, agent_id = client
    response = test_client.put(
        f"/api/access/agents/{agent_id}", json={"granted": ["piedpiper_acmeuni.read"], "mode": "admin"}
    )
    assert response.status_code == 422


def test_put_agents_invalid_expiry_format(client: tuple[TestClient, CapabilityStore, str]) -> None:
    test_client, _caps, agent_id = client
    response = test_client.put(
        f"/api/access/agents/{agent_id}",
        json={"granted": ["piedpiper_acmeuni.read"], "expires_at": "not-a-timestamp"},
    )
    assert response.status_code == 422
