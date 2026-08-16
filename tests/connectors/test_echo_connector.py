"""U-E3: credential-free Echo first-light ceremony and counterfeits."""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest

from omniagentos.connectors import load_registry
from omniagentos.connectors.broker import AuditContext, BrokerDenied, call
from omniagentos.connectors.store import CapabilityStore
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore

AGENT_ID = "lane:echo.firstlight"
RUN_ID = "run-echo-firstlight"


@pytest.fixture
def capability_store(tmp_path: Path) -> CapabilityStore:
    db_path = str(tmp_path / "echo.sqlite3")
    migrate(db_path)
    raw = SqliteStore(db_path)
    raw._connection.execute(
        "INSERT INTO agents (id, name, lineage, model, expertise_json, trust_level, "
        "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            AGENT_ID,
            AGENT_ID,
            "test.echo",
            None,
            "[]",
            "T1",
            "idle",
            "2026-08-03T00:00:00+00:00",
            "2026-08-03T00:00:00+00:00",
        ),
    )
    raw._connection.commit()
    try:
        yield CapabilityStore(raw)
    finally:
        raw.close()


def _echo_call(
    store: CapabilityStore,
    token: str,
    request_id: str,
    body: object,
) -> dict[str, object]:
    identity = store.resolve_token(token)
    if identity is None:
        raise BrokerDenied(
            "credential_missing",
            "echo.ping",
            "the run credential is unknown, expired, or revoked",
        )
    agent_id = identity["agent_id"]
    return call(
        "echo.ping",
        store.get_grant(agent_id),
        method="POST",
        path="/ping",
        body=body,
        grant_store=store,
        agent_id=agent_id,
        audit_store=store,
        audit_context=AuditContext(
            holder=AGENT_ID,
            run_id=identity["run_id"],
            request_id=request_id,
        ),
    )


def test_echo_registry_is_callable_without_credentials() -> None:
    registry = load_registry()
    connector = registry.connectors["echo"]
    capability = registry.capability("echo.ping")

    assert connector.env == []
    assert capability.callable_now is True
    assert capability.resolved_read_only is True
    assert capability.http is not None
    assert capability.http.base_url == "inprocess://echo"


def test_full_first_light_ceremony_and_revoke_counterfeit(
    capability_store: CapabilityStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = str(uuid.uuid4())
    capability_store.issue_scoped_grant(
        AGENT_ID,
        ["echo.ping"],
        mode="read",
        expires_at="2099-01-01T00:00:00+00:00",
        issued_by="human:operator",
        request_id=request_id,
    )
    token = capability_store.issue_token(RUN_ID, AGENT_ID)
    assert capability_store.resolve_token(token) == {"run_id": RUN_ID, "agent_id": AGENT_ID}

    def network_is_a_counterfeit(*args: object, **kwargs: object) -> object:
        raise AssertionError("Echo must never use the network")

    monkeypatch.setattr(httpx, "request", network_is_a_counterfeit)
    result = _echo_call(capability_store, token, request_id, {"message": "first light"})
    assert result["body"] == {"pong": True, "message": "first light"}

    initial_rows = list(reversed(capability_store.call_log(RUN_ID)))
    assert [row["decision"] for row in initial_rows] == ["intent", "allowed"]
    assert len(initial_rows) == 2
    assert len({row["call_id"] for row in initial_rows}) == 1
    assert {row["request_id"] for row in initial_rows} == {request_id}
    assert sum(row["allowed"] for row in initial_rows) == 1

    capability_store.set_grant(AGENT_ID, [], actor="human:operator", note="ceremony revoke")
    with pytest.raises(BrokerDenied) as denied:
        _echo_call(capability_store, token, request_id, {"message": "after revoke"})
    assert denied.value.reason == "not_granted"

    rows = list(reversed(capability_store.call_log(RUN_ID)))
    assert [row["decision"] for row in rows] == ["intent", "allowed", "intent", "denied"]
    assert rows[-1]["reason_code"] == "not_granted"
    assert rows[-1]["allowed"] == 0
    assert {row["request_id"] for row in rows} == {request_id}
    assert sum(row["allowed"] for row in rows) == 1
    assert [entry["action"] for entry in reversed(capability_store.grant_log(AGENT_ID))] == [
        "grant",
        "revoke",
    ]


def test_expired_run_token_resolves_none_and_denies_credential_missing(
    capability_store: CapabilityStore,
) -> None:
    capability_store.issue_scoped_grant(AGENT_ID, ["echo.ping"])
    token = capability_store.issue_token(RUN_ID, AGENT_ID, ttl_seconds=-1)
    assert capability_store.resolve_token(token) is None

    with pytest.raises(BrokerDenied) as denied:
        _echo_call(capability_store, token, str(uuid.uuid4()), {"message": "too late"})
    assert denied.value.reason == "credential_missing"


@pytest.mark.parametrize(
    "body",
    [None, {}, {"message": ""}, {"message": "ok", "extra": True}],
)
def test_malformed_echo_request_is_validation_denial_not_500(
    capability_store: CapabilityStore,
    body: object,
) -> None:
    capability_store.issue_scoped_grant(AGENT_ID, ["echo.ping"])
    token = capability_store.issue_token(RUN_ID, AGENT_ID)
    request_id = str(uuid.uuid4())

    with pytest.raises(BrokerDenied) as denied:
        _echo_call(capability_store, token, request_id, body)
    assert denied.value.reason == "validation_error"
    assert denied.value.payload()["reason_code"] == "validation_error"
    rows = list(reversed(capability_store.call_log(RUN_ID)))
    assert [row["decision"] for row in rows] == ["intent", "denied"]
    assert rows[-1]["reason_code"] == "validation_error"
