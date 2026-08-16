"""U-A1 broker-owned intent/final audit and outage counterfeits."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omniagentos.connectors import Capability, HttpSpec, broker
from omniagentos.connectors.broker import AuditContext, BrokerDenied
from omniagentos.connectors.store import CapabilityStore
from omniagentos.contracts import ActionClass
from omniagentos.db.store import SqliteStore


class _Response:
    status_code = 200
    is_success = True
    text = ""

    def json(self) -> dict[str, Any]:
        return {"ok": True}


def _cap(cap_id: str = "fixture.read", *, credentialed: bool = True) -> Capability:
    return Capability(
        id=cap_id,
        connector="fixture",
        group="support",
        label=cap_id,
        action_class=ActionClass.READ_ONLY,
        http=HttpSpec(
            base_url="https://fixture.test",
            methods=["GET"],
            auth="bearer:FIXTURE_ENV" if credentialed else "",
        ),
    )


def _registry(*capabilities: Capability) -> SimpleNamespace:
    indexed = {cap.id: cap for cap in capabilities}
    return SimpleNamespace(
        capability=lambda cap_id: indexed[cap_id],
        connectors={
            "fixture": SimpleNamespace(env=["FIXTURE_ENV"] if any(cap.http.auth for cap in capabilities) else [])
        },
        groups={"support": SimpleNamespace(danger=False)},
    )


@pytest.fixture
def audit_store(tmp_path: Path) -> tuple[SqliteStore, CapabilityStore]:
    raw = SqliteStore(str(tmp_path / "audit.sqlite3"))
    try:
        yield raw, CapabilityStore(raw)
    finally:
        raw.close()


def _context() -> AuditContext:
    return AuditContext(
        holder="lane:runner.step",
        run_id="run-fixture",
        task_id="task-fixture",
        session_id="session-fixture",
        request_id="req-fixture",
        grant_id="grn-fixture",
        grant_issuer="human:operator",
        grant_expires_at="2099-01-01T00:00:00Z",
        correlation_id="corr-fixture",
    )


def _rows(store: CapabilityStore) -> list[dict[str, Any]]:
    return list(reversed(store.call_log()))


def test_non_loop_call_writes_exactly_one_intent_final_pair_without_values(
    monkeypatch: pytest.MonkeyPatch,
    audit_store: tuple[SqliteStore, CapabilityStore],
) -> None:
    _raw, calls = audit_store
    capability = _cap()
    forbidden = re.compile(r"(?i)(password|secret|key|token|credential|auth)")
    generated_value = f"{forbidden.pattern}-{uuid.uuid4().hex}"
    monkeypatch.setattr(broker, "load_registry", lambda: _registry(capability))
    monkeypatch.setenv("FIXTURE_ENV", generated_value)
    monkeypatch.setattr(httpx, "request", lambda *args, **kwargs: _Response())

    result = broker.call(
        capability.id,
        [capability.id],
        audit_store=calls,
        audit_context=_context(),
    )

    assert result["ok"] is True
    rows = _rows(calls)
    assert [row["decision"] for row in rows] == ["intent", "allowed"]
    assert len({row["call_id"] for row in rows}) == 1
    for row in rows:
        assert row["call_id"]
        assert row["ts"]
        assert row["run_id"] == "run-fixture"
        assert row["task_id"] == "task-fixture"
        assert row["session_id"] == "session-fixture"
        assert row["holder"] == "lane:runner.step"
        assert row["capability_id"] == capability.id
        assert row["action_mode"] == "read"
        assert row["connector"] == "fixture"
        assert row["env_name"] == "FIXTURE_ENV"
        assert row["grant_id"] == "grn-fixture"
        assert row["latency_ms"] >= 0

    assert generated_value not in {str(value) for row in rows for value in row.values()}
    assert [
        value
        for row in rows
        for value in row.values()
        if value is not None and forbidden.search(str(value))
    ] == []


def test_denied_call_is_paired_with_reason_and_allowed_zero(
    monkeypatch: pytest.MonkeyPatch,
    audit_store: tuple[SqliteStore, CapabilityStore],
) -> None:
    _raw, calls = audit_store
    capability = _cap()
    monkeypatch.setattr(broker, "load_registry", lambda: _registry(capability))

    with pytest.raises(BrokerDenied) as denied:
        broker.call(
            capability.id,
            [],
            audit_store=calls,
            audit_context=_context(),
        )

    assert denied.value.reason == "not_granted"
    rows = _rows(calls)
    assert [row["decision"] for row in rows] == ["intent", "denied"]
    assert rows[-1]["allowed"] == 0
    assert rows[-1]["reason_code"] == "not_granted"


def test_authorized_pre_wire_transport_failure_finalizes_failed(
    monkeypatch: pytest.MonkeyPatch,
    audit_store: tuple[SqliteStore, CapabilityStore],
) -> None:
    _raw, calls = audit_store
    capability = _cap()
    monkeypatch.setattr(broker, "load_registry", lambda: _registry(capability))
    monkeypatch.setenv("FIXTURE_ENV", "runtime-only-fixture")

    def fail_before_wire(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("fixture network unavailable")

    monkeypatch.setattr(httpx, "request", fail_before_wire)
    with pytest.raises(BrokerDenied) as denied:
        broker.call(
            capability.id,
            [capability.id],
            audit_store=calls,
            audit_context=_context(),
        )

    assert denied.value.reason == "transport_error"
    rows = _rows(calls)
    assert [row["decision"] for row in rows] == ["intent", "failed"]
    assert rows[-1]["reason_code"] == "transport_error"


def test_locked_audit_denies_credential_use_bounded_and_emits_health_while_free_call_runs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    audit_store: tuple[SqliteStore, CapabilityStore],
) -> None:
    raw, calls = audit_store
    credentialed = _cap()
    credential_free = _cap("fixture.echo", credentialed=False)
    monkeypatch.setattr(broker, "load_registry", lambda: _registry(credentialed, credential_free))
    monkeypatch.setenv("FIXTURE_ENV", "runtime-only-fixture")
    sent: list[str] = []

    def request(method: str, url: str, **kwargs: Any) -> _Response:
        sent.append(url)
        return _Response()

    monkeypatch.setattr(httpx, "request", request)
    lock = sqlite3.connect(raw._db_path, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with caplog.at_level(logging.ERROR, logger=broker.__name__):
            with pytest.raises(BrokerDenied) as denied:
                broker.call(
                    credentialed.id,
                    [credentialed.id],
                    audit_store=calls,
                    audit_context=_context(),
                )
        elapsed = time.monotonic() - started
        assert denied.value.reason == "audit_unavailable"
        assert 1.5 <= elapsed < 2.5
        assert sent == []
        assert "broker_audit_unavailable" in caplog.text

        result = broker.call(
            credential_free.id,
            [credential_free.id],
            audit_store=calls,
            audit_context=_context(),
        )
        assert result["ok"] is True
        assert len(sent) == 1
    finally:
        lock.rollback()
        lock.close()


def test_direct_resolution_is_also_paired(
    monkeypatch: pytest.MonkeyPatch,
    audit_store: tuple[SqliteStore, CapabilityStore],
) -> None:
    _raw, calls = audit_store
    capability = _cap()
    monkeypatch.setattr(broker, "load_registry", lambda: _registry(capability))
    monkeypatch.setenv("FIXTURE_ENV", "runtime-only-fixture")

    assert broker.resolve_for(
        capability,
        audit_store=calls,
        audit_context=_context(),
    ) == {"FIXTURE_ENV": "runtime-only-fixture"}
    rows = _rows(calls)
    assert [row["decision"] for row in rows] == ["intent", "allowed"]
    assert {row["method"] for row in rows} == {"RESOLVE"}


def test_single_name_resolution_is_also_paired(
    monkeypatch: pytest.MonkeyPatch,
    audit_store: tuple[SqliteStore, CapabilityStore],
) -> None:
    """Optional caller preflights retain the broker's durable audit trail."""
    _raw, calls = audit_store
    capability = _cap()
    monkeypatch.setattr(broker, "load_registry", lambda: _registry(capability))
    monkeypatch.setenv("FIXTURE_ENV", "runtime-only-fixture")

    assert (
        broker.resolve_one_for(
            capability,
            "FIXTURE_ENV",
            audit_store=calls,
            audit_context=_context(),
        )
        == "runtime-only-fixture"
    )
    rows = _rows(calls)
    assert [row["decision"] for row in rows] == ["intent", "allowed"]
    assert {row["method"] for row in rows} == {"RESOLVE"}


def test_a_store_permission_refusal_is_finalized_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    audit_store: tuple[SqliteStore, CapabilityStore],
    tmp_path: Path,
) -> None:
    """The U-S1 guard's refusal is recorded by the spine, not swallowed by it.

    The guard is armed inside the (now private) resolver, which sits UNDER
    ``resolve_for``. If the spine only recognised the four scope/provisioning
    codes, a store-permission refusal would either finalize as a generic
    ``unknown``/``internal_error`` or leave the pair unclosed — i.e. the one
    denial class that means "this box's secret storage is unsafe" would be the
    one class the audit trail could not name.
    """
    from omniagentos.security.secret_storage import (
        StoragePermissionGuard,
        register_permission_guard,
    )

    _raw, calls = audit_store
    capability = _cap()
    monkeypatch.setattr(broker, "load_registry", lambda: _registry(capability))
    monkeypatch.setenv("FIXTURE_ENV", "runtime-only-fixture")

    store_dir = tmp_path / "store"
    store_dir.mkdir(mode=0o700)
    store_file = store_dir / "connections.env"
    store_file.write_text("PLACEHOLDER=generated-in-test")
    store_file.chmod(0o644)
    monkeypatch.setattr(broker, "_get_store_path_for_env", lambda _env: str(store_file))
    monkeypatch.setenv(broker.STORE_PERMISSION_GUARD_ENV, "enforce")
    register_permission_guard(StoragePermissionGuard())
    try:
        with pytest.raises(BrokerDenied) as denied:
            broker.resolve_for(capability, audit_store=calls, audit_context=_context())
    finally:
        register_permission_guard(None)

    assert denied.value.reason == "credential_unavailable"
    rows = _rows(calls)
    assert [row["decision"] for row in rows] == ["intent", "unavailable"]
    assert rows[-1]["reason_code"] == "credential_unavailable"
    assert rows[-1]["allowed"] == 0
    # Metadata only: neither the store path nor its contents may reach a row.
    assert str(store_file) not in str(rows)
    assert "runtime-only-fixture" not in str(rows)


class _FailAfterIntent:
    """An audit sink that accepts the intent row and loses every one after it.

    Reproduces the narrow window K1 is about: the durable intent IS written, so
    the call proceeds and the provider is asked; only the terminal write fails.
    """

    def __init__(self, inner: CapabilityStore) -> None:
        self._inner = inner
        self.writes = 0

    def log_call(self, *args: Any, **kwargs: Any) -> None:
        self.writes += 1
        if self.writes == 1:
            self._inner.log_call(*args, **kwargs)
            return
        raise sqlite3.OperationalError("database is locked")


def test_a_finalization_failure_after_a_successful_call_is_not_called_unbilled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    audit_store: tuple[SqliteStore, CapabilityStore],
) -> None:
    """The request WENT OUT. Its audit failure may not claim provable absence.

    ``audit_unavailable`` is the loop seam's vocabulary for "nothing left this
    process", and it releases the open budget reservation. Raising it here — a
    201 the provider is already billing for — is a double-spend licence, so the
    post-request position gets its own reason with its own remedy.
    """
    _raw, calls = audit_store
    capability = _cap()
    monkeypatch.setattr(broker, "load_registry", lambda: _registry(capability))
    monkeypatch.setenv("FIXTURE_ENV", "runtime-only-fixture")
    sent: list[str] = []

    def request(method: str, url: str, **kwargs: Any) -> _Response:
        sent.append(url)
        return _Response()

    monkeypatch.setattr(httpx, "request", request)
    sink = _FailAfterIntent(calls)

    with caplog.at_level(logging.ERROR, logger=broker.__name__):
        with pytest.raises(BrokerDenied) as denied:
            broker.call(
                capability.id,
                [capability.id],
                audit_store=sink,
                audit_context=_context(),
            )

    assert sent, "the call must actually have been issued for this window to exist"
    assert denied.value.reason == "audit_finalization_failed"
    assert denied.value.reason != "audit_unavailable"
    assert denied.value.next_action, "a typed refusal must carry an actionable remedy"
    assert "broker_audit_unavailable" in caplog.text
    # The intent row survives, so the orphan is forensically visible.
    assert [row["decision"] for row in _rows(calls)] == ["intent"]


def test_a_finalization_failure_for_a_call_that_never_went_out_stays_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    audit_store: tuple[SqliteStore, CapabilityStore],
) -> None:
    """The counterweight: a locally-decided denial issued nothing, so it refunds.

    Without this, "mark every audit failure may-have-billed" would pass, and
    the loop's daily cap would be eaten by calls that never opened a socket.
    """
    _raw, calls = audit_store
    capability = _cap()
    monkeypatch.setattr(broker, "load_registry", lambda: _registry(capability))
    monkeypatch.setenv("FIXTURE_ENV", "runtime-only-fixture")
    sent: list[str] = []
    monkeypatch.setattr(httpx, "request", lambda *a, **k: sent.append(k) or _Response())
    sink = _FailAfterIntent(calls)

    with pytest.raises(BrokerDenied) as denied:
        # Not in the granted list: refused before the request is ever built.
        broker.call(
            capability.id,
            [],
            audit_store=sink,
            audit_context=_context(),
        )

    assert sent == []
    assert denied.value.reason == "audit_unavailable"
