"""Call-time lifecycle enforcement for capability grants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omniagentos.collab.contracts import Agent
from omniagentos.collab.store import CollabStore
from omniagentos.connectors import Capability, HttpSpec, broker
from omniagentos.connectors.broker import AuditContext, BrokerDenied, authorize
from omniagentos.connectors.broker import call as broker_call
from omniagentos.connectors.store import CapabilityStore
from omniagentos.contracts import ActionClass
from omniagentos.db.store import SqliteStore
from omniagentos.grants.store import ConsumeResult


@pytest.fixture
def cap_store(tmp_path) -> tuple[CapabilityStore, str]:
    collab = CollabStore(db_path=str(tmp_path / "broker-lifecycle.db"))
    agent = Agent(name="broker-lifecycle-agent")
    collab.register_agent(agent)
    return CapabilityStore(collab._store), agent.id


def _authorize(store: CapabilityStore, agent_id: str, capability_id: str, **kwargs: str) -> Capability:
    return authorize(
        capability_id,
        store.get_grant(agent_id),
        grant_store=store,
        agent_id=agent_id,
        **kwargs,
    )


def test_authorize_expired_grant_denied(cap_store: tuple[CapabilityStore, str]) -> None:
    store, agent_id = cap_store
    store.issue_scoped_grant(
        agent_id,
        ["piedpiper_acmeuni.read"],
        expires_at=(datetime.now(tz=UTC) - timedelta(seconds=1)).isoformat(),
    )
    with pytest.raises(BrokerDenied, match="grant_expired") as exc:
        _authorize(store, agent_id, "piedpiper_acmeuni.read")
    assert exc.value.reason == "grant_expired"


def test_authorize_future_expiry_allowed(cap_store: tuple[CapabilityStore, str]) -> None:
    store, agent_id = cap_store
    store.issue_scoped_grant(
        agent_id,
        ["piedpiper_acmeuni.read"],
        expires_at=(datetime.now(tz=UTC) + timedelta(days=1)).isoformat(),
    )
    assert _authorize(store, agent_id, "piedpiper_acmeuni.read").id == "piedpiper_acmeuni.read"


def _write_capability() -> Capability:
    """A callable payment write that is not a hard-human class, for scope testing."""
    return Capability(
        id="piedpiper_acmeuni.note_write",
        connector="scope-test",
        group="payments",
        label="Scope test write",
        action_class=ActionClass.INTERNAL_REVERSIBLE,
        http=HttpSpec(base_url="https://example.test", methods=["POST"], path_prefixes=["/"]),
    )


def test_authorize_read_mode_write_capability_denied(
    cap_store: tuple[CapabilityStore, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, agent_id = cap_store
    store.issue_scoped_grant(agent_id, ["piedpiper_acmeuni.note_write"], mode="read")
    cap = _write_capability()
    monkeypatch.setattr(broker, "load_registry", lambda: type("Registry", (), {"capability": lambda *_: cap})())
    with pytest.raises(BrokerDenied, match="mode_denied") as exc:
        _authorize(store, agent_id, cap.id, method="POST", path="/")
    assert exc.value.reason == "mode_denied"


def test_authorize_write_mode_write_capability_allowed(
    cap_store: tuple[CapabilityStore, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, agent_id = cap_store
    store.issue_scoped_grant(agent_id, ["piedpiper_acmeuni.note_write"], mode="write")
    cap = _write_capability()
    monkeypatch.setattr(broker, "load_registry", lambda: type("Registry", (), {"capability": lambda *_: cap})())
    assert _authorize(store, agent_id, cap.id, method="POST", path="/") == cap


def test_authorize_null_expiry_is_no_expiry(cap_store: tuple[CapabilityStore, str]) -> None:
    store, agent_id = cap_store
    store.issue_scoped_grant(agent_id, ["piedpiper_acmeuni.read"])
    assert _authorize(store, agent_id, "piedpiper_acmeuni.read").id == "piedpiper_acmeuni.read"


# --- writer:broker-dry-run -------------------------------------------------
#
# The authorize-only pre-flight. Two properties carry the whole capability and
# each is asserted on the ABSENCE of an effect, never on a return value alone:
#
#   1. A dry run mutates nothing -- no HTTP request, no in-process dispatch, no
#      credential resolution, no bounded-grant consumption.
#   2. A dry run refuses everything a real call refuses. Skipping the atomic
#      consume must not launder a grant the real path would reject, and a
#      verdict that cannot be reached is a refusal, not permission.

_PAYOUT_CAP_ID = "dryrunfixture.payout"
_GRANT_ID = "grn-dryrun-fixture"


class _SpyExploded(AssertionError):
    """Raised by every side-effect spy: reaching one is the failure."""


def _payout_capability() -> Capability:
    """A credentialed payments WRITE: the shape where a mistake is money."""
    return Capability(
        id=_PAYOUT_CAP_ID,
        connector="dryrunfixture",
        group="payments",
        label="Dry-run fixture payout",
        action_class=ActionClass.INTERNAL_REVERSIBLE,
        http=HttpSpec(
            base_url="https://payouts.dryrun.test",
            methods=["POST"],
            path_prefixes=["/v1/payouts"],
            auth="bearer:DRYRUN_FIXTURE_ENV",
        ),
    )


def _dry_run_registry(capability: Capability) -> SimpleNamespace:
    return SimpleNamespace(
        capability=lambda cap_id: capability,
        connectors={"dryrunfixture": SimpleNamespace(env=["DRYRUN_FIXTURE_ENV"])},
        groups={"payments": SimpleNamespace(danger=True)},
    )


def _live_grant_row(capability: Capability, **overrides: Any) -> dict[str, Any]:
    row = {
        "id": _GRANT_ID,
        "capability": capability.id,
        "revoked_at": None,
        "plan_approval_state": "approved",
        "expires_at": (datetime.now(tz=UTC) + timedelta(days=1)).isoformat(),
        "max_actions": 5,
        "actions_used": 0,
        "max_spend_usd": 100.0,
        "spend_used_usd": 0.0,
        "target_set": [],
        "metadata": {"generation": 7, "action_class": capability.action_class.value},
    }
    row.update(overrides)
    return row


class _CountingGrantStore:
    """A campaign-grant store that counts consumption instead of performing it.

    ``try_consume`` succeeds so that a broken implementation proceeds to the
    dispatch spies rather than dying here -- the red-first failure then names
    the real defect (a dry run that dispatched) instead of masking it.
    """

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.consume_calls: list[dict[str, Any]] = []

    def get_grant(self, grant_id: str) -> dict[str, Any] | None:
        return dict(self.row) if grant_id == self.row["id"] else None

    def try_consume(self, grant_id: str, **kwargs: Any) -> ConsumeResult:
        self.consume_calls.append({"grant_id": grant_id, **kwargs})
        self.row["actions_used"] = int(self.row["actions_used"]) + 1
        return ConsumeResult(True, "ok", grant=dict(self.row))


@pytest.fixture
def dry_run_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Capability, _CountingGrantStore, CapabilityStore, dict[str, list[str]]]:
    """Wire the broker to a fixture payments capability with every sink spied."""
    capability = _payout_capability()
    monkeypatch.setattr(broker, "load_registry", lambda: _dry_run_registry(capability))
    monkeypatch.setenv("DRYRUN_FIXTURE_ENV", "unused-by-a-preflight")

    tripped: dict[str, list[str]] = {"http": [], "secret": [], "echo": []}

    def _no_http(*_args: Any, **_kwargs: Any) -> Any:
        tripped["http"].append("httpx")
        raise _SpyExploded("a dry run must never issue an HTTP request")

    def _no_secret(*_args: Any, **_kwargs: Any) -> Any:
        tripped["secret"].append("_auth_headers")
        raise _SpyExploded("a dry run must never resolve a credential")

    def _no_echo(*_args: Any, **_kwargs: Any) -> Any:
        tripped["echo"].append("_dispatch_echo")
        raise _SpyExploded("a dry run must never dispatch in-process")

    monkeypatch.setattr(httpx, "request", _no_http)
    monkeypatch.setattr(httpx, "Client", _no_http)
    monkeypatch.setattr(broker, "_auth_headers", _no_secret)
    monkeypatch.setattr(broker, "_auth_form", _no_secret)
    monkeypatch.setattr(
        "omniagentos.provision.connectors.echo._dispatch_echo",
        _no_echo,
    )

    raw = SqliteStore(str(tmp_path / "dry-run-audit.sqlite3"))
    try:
        yield capability, _CountingGrantStore(_live_grant_row(capability)), CapabilityStore(raw), tripped
    finally:
        raw.close()


def _audit_decisions(calls: CapabilityStore) -> list[str]:
    return [row["decision"] for row in reversed(calls.call_log())]


def _preflight(
    capability: Capability,
    grants: _CountingGrantStore,
    calls: CapabilityStore,
    **overrides: Any,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "method": "POST",
        "path": "/v1/payouts",
        "body": {"amount": 25},
        "grant_id": _GRANT_ID,
        "grant_store": grants,
        "generation": 7,
        "audit_store": calls,
        "audit_context": AuditContext(holder="lane:dry-run", run_id="run-dry-run"),
        "dry_run": True,
    }
    kwargs.update(overrides)
    return broker_call(capability.id, [capability.id], **kwargs)


@pytest.mark.nsc_writer_proof("writer:broker-dry-run")
def test_dry_run_authorizes_without_dispatch_secret_or_consume(
    dry_run_env: tuple[Capability, _CountingGrantStore, CapabilityStore, dict[str, list[str]]],
) -> None:
    """The carrier: a full authorization verdict with zero side effects."""
    capability, grants, calls, tripped = dry_run_env

    receipt = _preflight(capability, grants, calls)

    # (a) the receipt is an honest pre-flight, not a response
    assert receipt["dry_run"] is True
    assert receipt["ok"] is True
    assert receipt["status"] == 0
    assert receipt["body"] is None
    assert receipt["capability"] == capability.id

    # (b) no dispatch and no credential resolution -- asserted on the spies
    assert tripped == {"http": [], "secret": [], "echo": []}

    # (c) the bounded grant was neither consumed nor mutated
    assert grants.consume_calls == []
    assert grants.row["actions_used"] == 0
    assert grants.row["spend_used_usd"] == 0.0

    # (d) the audit spine records exactly intent + dry_run, and the boolean
    #     ``allowed`` column stays False so a pre-flight can never be read back
    #     as a call that ran.
    rows = list(reversed(calls.call_log()))
    assert [row["decision"] for row in rows] == ["intent", "dry_run"]
    assert len({row["call_id"] for row in rows}) == 1
    assert [bool(row["allowed"]) for row in rows] == [False, False]


@pytest.mark.nsc_writer_proof("writer:broker-dry-run")
def test_dry_run_of_money_write_without_durable_grant_is_refused(
    dry_run_env: tuple[Capability, _CountingGrantStore, CapabilityStore, dict[str, list[str]]],
) -> None:
    """The hard-stops genuinely evaluate on the pre-flight path.

    A dry run that ignored them would certify calls the real path refuses --
    the pre-flight would then be worse than no pre-flight.
    """
    capability, grants, calls, tripped = dry_run_env

    with pytest.raises(BrokerDenied) as exc:
        _preflight(capability, grants, calls, grant_id=None, grant_store=None, generation=None)

    assert exc.value.reason == "money_write_requires_approval"
    assert tripped == {"http": [], "secret": [], "echo": []}
    assert grants.consume_calls == []
    assert _audit_decisions(calls) == ["intent", "denied"]


def test_dry_run_does_not_launder_a_grant_the_real_path_would_refuse(
    dry_run_env: tuple[Capability, _CountingGrantStore, CapabilityStore, dict[str, list[str]]],
) -> None:
    """Skipping the atomic consume must not skip the consume's SCOPE check.

    ``try_consume`` validates (generation / target / pins / headroom) and only
    then spends. The pre-flight runs the same validator read-only, so an
    exhausted grant is refused rather than pre-approved.
    """
    capability, grants, calls, tripped = dry_run_env
    grants.row["actions_used"] = grants.row["max_actions"]

    with pytest.raises(BrokerDenied) as exc:
        _preflight(capability, grants, calls)

    assert exc.value.reason == "grant_refused"
    assert exc.value.detail == "max_actions"
    assert grants.consume_calls == []
    assert tripped == {"http": [], "secret": [], "echo": []}


def test_dry_run_with_a_generation_fence_mismatch_is_refused(
    dry_run_env: tuple[Capability, _CountingGrantStore, CapabilityStore, dict[str, list[str]]],
) -> None:
    """An UNKNOWN generation is a refusal, not a waiver.

    The durable fence is the classic favourable-absence surface: a pre-flight
    that treated a missing pin as "nothing to check" would bless a stale-fence
    call the real consume rejects.
    """
    capability, grants, calls, _tripped = dry_run_env
    grants.row["metadata"] = {"action_class": capability.action_class.value}

    with pytest.raises(BrokerDenied) as exc:
        _preflight(capability, grants, calls)

    assert exc.value.reason == "grant_refused"
    assert exc.value.detail == "generation_ambiguous"
    assert grants.consume_calls == []


def test_dry_run_refuses_when_the_grant_store_cannot_answer(
    dry_run_env: tuple[Capability, _CountingGrantStore, CapabilityStore, dict[str, list[str]]],
) -> None:
    """An unreadable store denies. Absence of an answer is never permission."""
    capability, grants, calls, tripped = dry_run_env

    class _BrokenStore:
        consume_calls: list[dict[str, Any]] = []

        def get_grant(self, _grant_id: str) -> dict[str, Any] | None:
            raise RuntimeError("grant store is down")

    with pytest.raises(BrokerDenied) as exc:
        _preflight(capability, grants, calls, grant_store=_BrokenStore())

    assert exc.value.reason == "grant_store_unavailable"
    assert tripped == {"http": [], "secret": [], "echo": []}
    assert grants.consume_calls == []


def test_dry_run_refuses_when_the_scope_validator_cannot_render_a_verdict(
    dry_run_env: tuple[Capability, _CountingGrantStore, CapabilityStore, dict[str, list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validator that raises or answers nothing refuses, never allows."""
    capability, grants, calls, tripped = dry_run_env

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("validator unavailable")

    monkeypatch.setattr("omniagentos.grants.validation.validate_approval_token", _explode)

    with pytest.raises(BrokerDenied) as exc:
        _preflight(capability, grants, calls)

    assert exc.value.reason == "grant_refused"
    assert grants.consume_calls == []
    assert tripped == {"http": [], "secret": [], "echo": []}

    monkeypatch.setattr(
        "omniagentos.grants.validation.validate_approval_token",
        lambda *_args, **_kwargs: object(),
    )
    with pytest.raises(BrokerDenied) as silent:
        _preflight(capability, grants, calls)
    assert silent.value.reason == "grant_refused"
    assert grants.consume_calls == []


@pytest.mark.parametrize("flag", ["true", "false", 1, 0, None, object()])
def test_a_non_boolean_dry_run_flag_refuses_before_anything_runs(
    dry_run_env: tuple[Capability, _CountingGrantStore, CapabilityStore, dict[str, list[str]]],
    flag: Any,
) -> None:
    """A misread pre-flight flag is a live send. Refuse instead of guessing."""
    capability, grants, calls, tripped = dry_run_env

    with pytest.raises(BrokerDenied) as exc:
        _preflight(capability, grants, calls, dry_run=flag)

    assert exc.value.reason == "invalid_dry_run_flag"
    assert tripped == {"http": [], "secret": [], "echo": []}
    assert grants.consume_calls == []
    assert calls.call_log() == []


def test_only_the_literal_false_suppresses_grant_consumption(
    dry_run_env: tuple[Capability, _CountingGrantStore, CapabilityStore, dict[str, list[str]]],
) -> None:
    """``consume``'s dangerous direction is a live call that spent nothing.

    So a falsy-but-not-False value consumes, and the pre-flight is selected by
    the literal ``False`` alone.
    """
    capability, grants, _calls, _tripped = dry_run_env

    broker.authorize_with_grant(
        capability.id,
        [capability.id],
        grants,
        _GRANT_ID,
        generation=7,
        method="POST",
        path="/v1/payouts",
        consume=0,
    )
    assert len(grants.consume_calls) == 1

    broker.authorize_with_grant(
        capability.id,
        [capability.id],
        grants,
        _GRANT_ID,
        generation=7,
        method="POST",
        path="/v1/payouts",
        consume=False,
    )
    assert len(grants.consume_calls) == 1


def test_dry_run_leaves_the_uncredentialed_and_in_process_sinks_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The short-circuit sits ABOVE every dispatch site, not just httpx.

    ``broker.call`` has two arms -- the uncredentialed fast path and the
    audited path -- and ``_call_unlogged`` has two sinks, the in-process echo
    and httpx. This walks the arm and the sink the carrier test does not.
    """
    echo = Capability(
        id="echo.ping",
        connector="echo",
        group="support",
        label="Echo",
        action_class=ActionClass.READ_ONLY,
        http=HttpSpec(base_url="inprocess://echo", methods=["POST"], path_prefixes=["/"]),
    )
    dispatched: list[Any] = []
    monkeypatch.setattr(
        broker,
        "load_registry",
        lambda: SimpleNamespace(
            capability=lambda _cap_id: echo,
            connectors={"echo": SimpleNamespace(env=[])},
            groups={"support": SimpleNamespace(danger=False)},
        ),
    )
    monkeypatch.setattr(
        "omniagentos.provision.connectors.echo._dispatch_echo",
        lambda body: dispatched.append(body),
    )

    raw = SqliteStore(str(tmp_path / "echo-audit.sqlite3"))
    try:
        calls = CapabilityStore(raw)
        receipt = broker_call(
            echo.id,
            [echo.id],
            method="POST",
            path="/",
            body={"msg": "hi"},
            audit_store=calls,
            audit_context=AuditContext(holder="lane:dry-run"),
            dry_run=True,
        )
        assert receipt["dry_run"] is True
        assert receipt["body"] is None
        assert dispatched == []
        assert _audit_decisions(calls) == ["intent", "dry_run"]
    finally:
        raw.close()


def test_a_real_call_still_dispatches_and_consumes(
    dry_run_env: tuple[Capability, _CountingGrantStore, CapabilityStore, dict[str, list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anti-vacuity: without ``dry_run`` the very same request DOES dispatch."""
    capability, grants, calls, _tripped = dry_run_env
    sent: list[str] = []

    class _Response:
        status_code = 200
        is_success = True
        text = ""

        def json(self) -> dict[str, Any]:
            return {"ok": True}

    monkeypatch.setattr(broker, "_auth_headers", lambda *_a, **_k: ({}, {}, None))
    monkeypatch.setattr(broker, "_auth_form", lambda *_a, **_k: {})
    monkeypatch.setattr(
        httpx,
        "request",
        lambda *args, **kwargs: (sent.append(str(args[0])), _Response())[1],
    )

    receipt = _preflight(capability, grants, calls, dry_run=False)

    assert sent == ["POST"]
    assert receipt.get("dry_run") is None
    assert len(grants.consume_calls) == 1
    assert _audit_decisions(calls) == ["intent", "allowed"]
