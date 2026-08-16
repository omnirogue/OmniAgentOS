"""Lifecycle scope persistence for capability grants."""

from __future__ import annotations

import sqlite3

import pytest

from omniagentos.collab.contracts import Agent
from omniagentos.collab.store import CollabStore
from omniagentos.connectors import ConnectorError
from omniagentos.connectors.store import CapabilityStore


@pytest.fixture
def cap_store(tmp_path) -> tuple[CapabilityStore, str]:
    collab = CollabStore(db_path=str(tmp_path / "grants.db"))
    agent = Agent(name="lifecycle-agent")
    collab.register_agent(agent)
    return CapabilityStore(collab._store), agent.id


def test_issue_scoped_grant_basic(cap_store: tuple[CapabilityStore, str]) -> None:
    store, agent_id = cap_store
    assert store.issue_scoped_grant(agent_id, ["piedpiper_acmeuni.read"]) == ["piedpiper_acmeuni.read"]
    assert store.get_grant_row(agent_id, "piedpiper_acmeuni.read")["mode"] == "read"  # type: ignore[index]


def test_issue_scoped_grant_write_mode(cap_store: tuple[CapabilityStore, str]) -> None:
    store, agent_id = cap_store
    store.issue_scoped_grant(agent_id, ["piedpiper_acmeuni.note_write"], mode="write")
    assert store.get_grant_row(agent_id, "piedpiper_acmeuni.note_write")["mode"] == "write"  # type: ignore[index]


def test_issue_scoped_grant_with_expiry(cap_store: tuple[CapabilityStore, str]) -> None:
    store, agent_id = cap_store
    expiry = "2030-01-02T03:04:05Z"
    store.issue_scoped_grant(agent_id, ["piedpiper_acmeuni.read"], expires_at=expiry)
    assert store.get_grant_row(agent_id, "piedpiper_acmeuni.read")["expires_at"] == expiry  # type: ignore[index]


def test_issue_scoped_grant_multiple_caps(cap_store: tuple[CapabilityStore, str]) -> None:
    store, agent_id = cap_store
    expiry = "2030-01-02T03:04:05+00:00"
    granted = store.issue_scoped_grant(
        agent_id, ["piedpiper_acmeuni.read", "piedpiper_acmeuni.note_write"], mode="write", expires_at=expiry
    )
    assert granted == ["piedpiper_acmeuni.note_write", "piedpiper_acmeuni.read"]
    assert all(store.get_grant_row(agent_id, cap)["mode"] == "write" for cap in granted)  # type: ignore[index]
    assert all(store.get_grant_row(agent_id, cap)["expires_at"] == expiry for cap in granted)  # type: ignore[index]


def test_issue_scoped_grant_validates_capability_ids(cap_store: tuple[CapabilityStore, str]) -> None:
    store, agent_id = cap_store
    with pytest.raises(ConnectorError, match="unknown capability"):
        store.issue_scoped_grant(agent_id, ["piedpiper_acmeuni.read", "unknown.capability"])
    assert store.get_grant(agent_id) == []


def test_issue_scoped_grant_validates_mode(cap_store: tuple[CapabilityStore, str]) -> None:
    store, agent_id = cap_store
    with pytest.raises(ConnectorError, match="mode"):
        store.issue_scoped_grant(agent_id, ["piedpiper_acmeuni.read"], mode="admin")


def test_issue_scoped_grant_log_entry(cap_store: tuple[CapabilityStore, str]) -> None:
    store, agent_id = cap_store
    store.issue_scoped_grant(
        agent_id,
        ["piedpiper_acmeuni.read"],
        expires_at="2030-01-02T03:04:05Z",
        issued_by="approval:apr_123",
        request_id="req_123",
    )
    entry = store.grant_log(agent_id)[0]
    assert entry["mode"] == "read"
    assert entry["expires_at"] == "2030-01-02T03:04:05Z"
    assert entry["issued_by"] == "approval:apr_123"
    assert entry["request_id"] == "req_123"


def test_issue_scoped_grant_transactional(cap_store: tuple[CapabilityStore, str]) -> None:
    store, agent_id = cap_store
    store._conn.execute(  # noqa: SLF001 - inject a database failure after the first insert.
        "CREATE TRIGGER abort_lifecycle_grant BEFORE INSERT ON agent_capabilities "
        "WHEN NEW.capability_id = 'knowledge.read' BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="boom"):
        store.issue_scoped_grant(agent_id, ["piedpiper_acmeuni.read", "knowledge.read"])
    assert store.get_grant(agent_id) == []
    assert store.grant_log(agent_id) == []


def test_get_grant_row(cap_store: tuple[CapabilityStore, str]) -> None:
    store, agent_id = cap_store
    store.issue_scoped_grant(agent_id, ["piedpiper_acmeuni.read"], issued_by="operator")
    row = store.get_grant_row(agent_id, "piedpiper_acmeuni.read")
    assert row is not None
    assert row["capability_id"] == "piedpiper_acmeuni.read"
    assert {"mode", "expires_at", "issued_by", "request_id"} <= set(row)
