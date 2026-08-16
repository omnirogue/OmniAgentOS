"""U-E1 — the typed CapabilityRequest spine, end to end on ONE uuid.

The positive path is deliberately ``tool{echo.ping}``: the credential-free,
network-free first-light capability from U-E3. It is the only capability in the
catalogue where "a worker asked, the floor granted, the broker called it" can be
proved without a secret, a network, a payment, or a write. Phase 3 swaps in
``web.search`` once U-N1 lands and its cost class is declared.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from omniagentos.connectors import load_registry
from omniagentos.connectors.broker import AuditContext, call
from omniagentos.connectors.store import CapabilityStore
from omniagentos.db.store import SqliteStore
from omniagentos.provision.capability_policy import CALLER_WALL_SECONDS, CapabilityPolicy
from omniagentos.provision.capability_requests import (
    ENVELOPE_FIELDS,
    MAX_RATIONALE_CHARS,
    CapabilityRequest,
    CapabilityRequestService,
    EnvelopeRejected,
    ExecutionFloorHint,
    ExtraAgentRequest,
    KeyScopeRequest,
    SkillRequest,
    ToolRequest,
)
from tests.support.db_template import make_store

WORKER = "lane:swarm.worker.research"
LANE = "lane:swarm.worker.research"
RATIONALE = "need a deterministic read to prove the request spine end to end"


class FrozenClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    db_path = str(tmp_path / "requests.sqlite3")
    raw = make_store(SqliteStore, db_path)
    raw._connection.execute(
        "INSERT INTO agents (id, name, lineage, model, expertise_json, trust_level, "
        "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (WORKER, WORKER, "test.requests", None, "[]", "T1", "idle",
         "2026-08-03T00:00:00+00:00", "2026-08-03T00:00:00+00:00"),
    )
    raw._connection.commit()
    try:
        yield raw
    finally:
        raw.close()


@pytest.fixture
def service(store: SqliteStore) -> CapabilityRequestService:
    return CapabilityRequestService(store, clock=FrozenClock())


def _envelope(payload: object = None, **overrides: object) -> CapabilityRequest:
    base: dict[str, object] = {
        "from_agent_id": WORKER,
        "from_lane": LANE,
        "rationale": RATIONALE,
        "request": payload or ToolRequest("echo.ping"),
    }
    base.update(overrides)
    if "request_id" not in base:
        base["request_id"] = str(uuid.uuid4())
    if "requested_at" not in base:
        base["requested_at"] = "2026-08-03T12:00:00+00:00"
    return CapabilityRequest(**base)  # type: ignore[arg-type]


def _worker_grants(store: SqliteStore, agent_id: str = WORKER) -> list[str]:
    """Grants held by THIS worker.

    Migration 108 seeds two real loop grants into ``agent_capabilities`` on every
    fresh database, so an unscoped ``COUNT(*)`` here would pass for the wrong
    reason -- or fail for one.
    """
    rows = store._connection.execute(
        "SELECT capability_id FROM agent_capabilities WHERE agent_id = ? ORDER BY capability_id",
        (agent_id,),
    ).fetchall()
    return [str(row["capability_id"]) for row in rows]


# --------------------------------------------------------------- the envelope


def test_the_envelope_has_no_field_an_emitter_may_not_supply() -> None:
    """Attribution and authority have nowhere to land in the envelope.

    §5: "the emitter never supplies granted_by, risk, action class, credentials,
    grant IDs, expiration, or its own effective grants." That is a claim about
    the TYPE, so it is tested against the type rather than against one caller.
    """
    assert ENVELOPE_FIELDS == {
        "request_id",
        "from_agent_id",
        "from_lane",
        "session_id",
        "run_id",
        "task_id",
        "requested_at",
        "rationale",
        "request",
        "inherited_floor",
    }
    forbidden = {
        "granted_by", "granted", "risk", "action_class", "credential", "credentials",
        "env", "env_name", "grant_id", "grant", "expires_at", "expiry", "mode",
        "approval_token", "issued_by", "effective_grants",
    }
    assert ENVELOPE_FIELDS & forbidden == set()


def test_the_envelope_is_immutable() -> None:
    envelope = _envelope()
    with pytest.raises(AttributeError):
        envelope.rationale = "rewritten"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        envelope.request.tool_id = "stripe_acmeuni.read"  # type: ignore[union-attr]


def test_the_three_canonical_identity_enforcers_agree() -> None:
    """The regex, the sibling write path, and migration 113's CHECK are one rule.

    Three enforcement points and one grammar is the invariant; three grammars
    that mostly agree is how ``system_impostor`` gets in somewhere.
    """
    from omniagentos.provision.capability_requests import _CANONICAL_IDENTITY
    from omniagentos.reliability.store import _is_canonical_identity

    cases = {
        "system": True,
        "lane:runner.step": True,
        "lane:swarm.worker.coding": True,
        "loop:w2_inbox_triage": True,
        "job:learnings-digest": True,
        "human:owner": True,
        "agent:bob": False,
        "system_impostor": False,
        "lane:": False,
        "human:-x": False,
        "lane:a:b": False,
        "": False,
        " system": False,
        "system ": False,
    }
    for value, expected in cases.items():
        assert bool(_CANONICAL_IDENTITY.fullmatch(value)) is expected, value
        assert _is_canonical_identity(value) is expected, value


@pytest.mark.parametrize(
    "identity", ["agent:bob", "system_impostor", "lane:", "human:", "lane:a:b", ""]
)
def test_migration_113_refuses_non_canonical_holders(
    store: SqliteStore, identity: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "INSERT INTO capability_requests (request_id, from_agent_id, from_lane, "
            "requested_at, rationale, request_kind, request_json) "
            "VALUES (?, ?, 'lane:sessions', '2026-08-03T12:00:00+00:00', 'why', 'tool', '{}')",
            (str(uuid.uuid4()), identity),
        )


def test_a_stored_envelope_can_never_be_rewritten(
    service: CapabilityRequestService, store: SqliteStore
) -> None:
    envelope = _envelope()
    service.submit(envelope, channel_principal=WORKER)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store._connection.execute(
            "UPDATE capability_requests SET rationale = 'rewritten' WHERE request_id = ?",
            (envelope.request_id,),
        )


# ------------------------------------------------------------- decisive path


def test_a_worker_emits_tool_echo_ping_and_observes_its_own_grant(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One uuid: request -> decision -> grant -> broker call, end to end.

    This is the ONE test that crosses into the broker, so it runs on the REAL
    clock. A frozen clock here would mint a grant whose 1,800s expiry is measured
    on one clock and checked by ``authorize`` on another -- a test that passes
    for an hour after it is written and then starts failing on the wall clock.
    """

    def network_is_a_counterfeit(*args: object, **kwargs: object) -> object:
        raise AssertionError("the first-light path must never touch the network")

    monkeypatch.setattr(httpx, "request", network_is_a_counterfeit)
    service = CapabilityRequestService(store)

    envelope = CapabilityRequest.new(
        from_agent_id=WORKER,
        from_lane=LANE,
        rationale=RATIONALE,
        request=ToolRequest("echo.ping"),
        run_id="run-ue1",
    )
    receipt = service.submit(envelope, channel_principal=WORKER)

    assert receipt.state == "granted"
    assert receipt.next_action == "retry_with_grant"
    assert receipt.granted_capability_ids == ("echo.ping",)
    assert receipt.grant_decision_ms is not None
    assert receipt.grant_decision_ms < 100, "the floor SLO is p50 <100ms"

    # The SAME worker now observes the effective grant and makes the call.
    caps = CapabilityStore(store)
    assert caps.get_grant(WORKER) == ["echo.ping"]
    result = call(
        "echo.ping",
        caps.get_grant(WORKER),
        method="POST",
        path="/ping",
        body={"message": "one uuid, four tables"},
        grant_store=caps,
        agent_id=WORKER,
        audit_store=caps,
        audit_context=AuditContext(holder=WORKER, run_id="run-ue1", request_id=envelope.request_id),
    )
    assert result["body"] == {"pong": True, "message": "one uuid, four tables"}

    # ONE uuid joins all four durable surfaces.
    rid = envelope.request_id
    conn = store._connection
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM capability_requests WHERE request_id = ?", (rid,)
    ).fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM capability_decisions WHERE request_id = ?", (rid,)
    ).fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM agent_capabilities WHERE request_id = ?", (rid,)
    ).fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM capability_grant_log WHERE request_id = ?", (rid,)
    ).fetchone()["n"] == 1
    broker_rows = conn.execute(
        "SELECT decision FROM broker_calls WHERE request_id = ? ORDER BY id", (rid,)
    ).fetchall()
    assert [row["decision"] for row in broker_rows] == ["intent", "allowed"]

    joined = conn.execute(
        "SELECT r.from_agent_id, r.request_kind, d.state, d.granted_by, g.capability_id "
        "FROM capability_requests r "
        "JOIN capability_decisions d ON d.request_id = r.request_id "
        "JOIN agent_capabilities g ON g.request_id = r.request_id "
        "WHERE r.request_id = ?",
        (rid,),
    ).fetchone()
    assert dict(joined) == {
        "from_agent_id": WORKER,
        "request_kind": "tool",
        "state": "granted",
        "granted_by": "autogrant",
        "capability_id": "echo.ping",
    }

    # AGENTS NEVER NAME A KEY: not one of the ~580 environment variable names
    # the registry declares may appear anywhere in the request or decision
    # ledger. Asserted against the WHOLE registry rather than against echo's
    # (empty) list, so the check still bites once a credentialed capability
    # rides this same spine.
    registry = load_registry()
    declared_env = {name for c in registry.connectors.values() for name in c.env}
    assert declared_env, "the registry must declare env names for this check to mean anything"
    ledger_text = " ".join(
        str(value)
        for table in ("capability_requests", "capability_decisions")
        for row in conn.execute(f"SELECT * FROM {table}").fetchall()
        for value in dict(row).values()
    )
    assert not [name for name in declared_env if name in ledger_text]
    assert registry.connectors["echo"].env == []


def test_a_replayed_request_id_is_idempotent(
    service: CapabilityRequestService, store: SqliteStore
) -> None:
    envelope = _envelope()
    first = service.submit(envelope, channel_principal=WORKER)
    second = service.submit(envelope, channel_principal=WORKER)

    assert (second.state, second.decided_at) == (first.state, first.decided_at)
    assert store._connection.execute(
        "SELECT COUNT(*) AS n FROM capability_requests"
    ).fetchone()["n"] == 1
    assert _worker_grants(store) == ["echo.ping"]


# ---------------------------------------------------------------- key_scope


def test_a_mixed_group_grants_only_the_read_ids_and_names_the_excluded(
    service: CapabilityRequestService,
) -> None:
    expansion = service.expand_key_scope("monitoring")
    assert "echo.ping" in expansion.granted
    assert expansion.excluded, "monitoring must contain a member the floor cannot grant"

    receipt = service.submit(_envelope(KeyScopeRequest("monitoring")), channel_principal=WORKER)
    assert receipt.state == "granted"
    assert receipt.granted_capability_ids == expansion.granted
    assert receipt.excluded_capability_ids == expansion.excluded
    assert set(receipt.granted_capability_ids) & set(receipt.excluded_capability_ids) == set()


def test_a_zero_expansion_group_is_a_typed_denial_not_a_silent_no_op(
    service: CapabilityRequestService, store: SqliteStore
) -> None:
    expansion = service.expand_key_scope("knowledge")
    assert expansion.granted == ()

    envelope = _envelope(KeyScopeRequest("knowledge"))
    receipt = service.submit(envelope, channel_principal=WORKER)

    assert receipt.state == "hard_rejected"
    assert receipt.reason_code == "empty_scope_expansion"
    assert receipt.next_action == "continue_degraded"
    assert set(receipt.excluded_capability_ids) == set(expansion.excluded)
    assert _worker_grants(store) == []


def test_a_danger_group_expands_to_nothing_and_names_every_member(
    service: CapabilityRequestService,
) -> None:
    """Payments reads are callable and read-only; the group is danger, so no."""
    expansion = service.expand_key_scope("payments")
    assert expansion.granted == ()
    assert "stripe_acmeuni.read" in expansion.excluded

    receipt = service.submit(_envelope(KeyScopeRequest("payments")), channel_principal=WORKER)
    assert receipt.state == "hard_rejected"
    assert receipt.reason_code == "empty_scope_expansion"


def test_an_unknown_group_is_refused_before_the_ledger(
    service: CapabilityRequestService, store: SqliteStore
) -> None:
    with pytest.raises(EnvelopeRejected) as rejected:
        service.submit(_envelope(KeyScopeRequest("everything")), channel_principal=WORKER)
    assert rejected.value.reason_code == "unknown_scope_group"
    assert store._connection.execute(
        "SELECT COUNT(*) AS n FROM capability_requests"
    ).fetchone()["n"] == 0


# ------------------------------------------------- skill / extra_agent park


def test_a_skill_request_parks_for_an_operator_and_hits_the_wall(
    store: SqliteStore,
) -> None:
    clock = FrozenClock()
    service = CapabilityRequestService(store, clock=clock)
    envelope = _envelope(SkillRequest("copywriting.brief"))
    parked = service.submit(envelope, channel_principal=WORKER)

    assert parked.state == "pending_operator"
    assert parked.next_action == "await_operator"
    assert parked.reason_code == "requires_operator:skill"

    clock.advance(CALLER_WALL_SECONDS)
    released = service.await_decision(envelope.request_id)
    assert released.state == "timed_out"
    assert released.next_action == "escalate_tier"


def test_an_extra_agent_request_parks_and_a_named_operator_can_decide_it(
    store: SqliteStore,
) -> None:
    service = CapabilityRequestService(store, clock=FrozenClock())
    envelope = _envelope(
        ExtraAgentRequest(formation="research", count=2, objective="widen the survey")
    )
    parked = service.submit(envelope, channel_principal=WORKER)
    assert (parked.state, parked.next_action) == ("pending_operator", "await_operator")

    denied = CapabilityPolicy(clock=FrozenClock()).operator_decide(
        store, envelope.request_id, decision="denied", operator="human:owner"
    )
    assert denied.state == "denied"
    assert denied.next_action == "continue_degraded"


@pytest.mark.parametrize(
    "payload",
    [
        ExtraAgentRequest(formation="research", count=5, objective="too many"),
        ExtraAgentRequest(formation="research", count=0, objective="none"),
        ExtraAgentRequest(formation="wrecking_crew", count=1, objective="unknown formation"),
        ExtraAgentRequest(formation="coding", count=1, objective="   "),
        ExtraAgentRequest(
            formation="coding", count=1, objective="escape", owned_paths=("../../etc",)
        ),
    ],
)
def test_extra_agent_bounds_are_refused_before_the_ledger(
    service: CapabilityRequestService, store: SqliteStore, payload: ExtraAgentRequest
) -> None:
    with pytest.raises(EnvelopeRejected):
        service.submit(_envelope(payload), channel_principal=WORKER)
    assert store._connection.execute(
        "SELECT COUNT(*) AS n FROM capability_requests"
    ).fetchone()["n"] == 0


def test_an_inherited_floor_is_carried_but_grants_nothing_by_itself(
    service: CapabilityRequestService, store: SqliteStore
) -> None:
    """A rung's floor is evidence of need; the delta still traverses the spine."""
    envelope = _envelope(
        SkillRequest("copywriting.brief"),
        inherited_floor=ExecutionFloorHint(
            tier="complex", effort="high", capabilities=("stripe_acmeuni.refund",)
        ),
    )
    receipt = service.submit(envelope, channel_principal=WORKER)
    assert receipt.state == "pending_operator"
    stored = service.get(envelope.request_id)
    assert stored is not None
    assert "stripe_acmeuni.refund" in str(stored["inherited_floor_json"])
    assert _worker_grants(store) == []


# ------------------------------------------------------------- COUNTERFEIT #1
#
# "Malformed tool_id='fake.tool' hard-rejects with NO grant and NO request row
#  (bounded validation event only)."


def test_counterfeit_fake_tool_creates_no_row_and_only_a_bounded_event(
    service: CapabilityRequestService, store: SqliteStore
) -> None:
    envelope = _envelope(ToolRequest("fake.tool"))
    with pytest.raises(EnvelopeRejected) as rejected:
        service.submit(envelope, channel_principal=WORKER)

    assert rejected.value.reason_code == "unaddressable_capability"
    for table in ("capability_requests", "capability_decisions"):
        assert store._connection.execute(
            f"SELECT COUNT(*) AS n FROM {table}"
        ).fetchone()["n"] == 0
    assert _worker_grants(store) == []
    assert store._connection.execute(
        "SELECT COUNT(*) AS n FROM capability_grant_log WHERE agent_id = ?", (WORKER,)
    ).fetchone()["n"] == 0

    assert len(service.validation_events) == 1
    event = service.validation_events[0]
    assert event == {
        "event": "validation_rejected",
        "reason_code": "unaddressable_capability",
        "subject": "fake.tool",
    }
    # The event echoes the bounded id and NOTHING the emitter wrote.
    assert RATIONALE not in " ".join(event.values())


def test_counterfeit_a_malformed_envelope_never_reaches_the_ledger(
    service: CapabilityRequestService, store: SqliteStore
) -> None:
    cases = [
        (_envelope(request_id="not-a-uuid"), "malformed_request_id"),
        (_envelope(from_agent_id="agent:bob", from_lane="agent:bob"), "non_canonical_identity"),
        (_envelope(from_lane="agent:bob"), "non_canonical_lane"),
        (_envelope(rationale=""), "malformed_rationale"),
        (_envelope(rationale="x" * (MAX_RATIONALE_CHARS + 1)), "malformed_rationale"),
        (_envelope(requested_at="yesterday"), "malformed_timestamp"),
        (_envelope(ToolRequest("NotACapability")), "malformed_capability_id"),
        (_envelope("just a string"), "impossible_union"),
    ]
    for envelope, expected in cases:
        with pytest.raises(EnvelopeRejected) as rejected:
            service.submit(envelope, channel_principal=envelope.from_agent_id)
        assert rejected.value.reason_code == expected, expected
    assert store._connection.execute(
        "SELECT COUNT(*) AS n FROM capability_requests"
    ).fetchone()["n"] == 0
    assert len(service.validation_events) == len(cases)


# ------------------------------------------------------------- COUNTERFEIT #2
#
# "A well-formed but unknown registered id is DURABLY hard_rejected as
#  unknown_capability."


def test_counterfeit_a_wellformed_unknown_id_is_durably_hard_rejected(
    service: CapabilityRequestService, store: SqliteStore
) -> None:
    envelope = _envelope(ToolRequest("echo.unknown"))
    receipt = service.submit(envelope, channel_principal=WORKER)

    assert receipt.state == "hard_rejected"
    assert receipt.reason_code == "unknown_capability"
    assert receipt.next_action == "stop_terminal"
    assert receipt.terminal is True

    # DURABLE -- the pre/post-validation boundary is exactly this difference from
    # `fake.tool`: a real connector, an unreal capability, so a real decision.
    assert service.get(envelope.request_id) is not None
    row = store._connection.execute(
        "SELECT * FROM capability_decisions WHERE request_id = ?", (envelope.request_id,)
    ).fetchone()
    assert row["state"] == "hard_rejected"
    assert row["reason_code"] == "unknown_capability"
    assert row["decided_at"] is not None
    assert len(service.validation_events) == 0
    assert _worker_grants(store) == []


# ------------------------------------------------------------- COUNTERFEIT #3
#
# "An envelope claiming a different from_agent_id than its channel principal is
#  rejected."


def test_counterfeit_a_forged_identity_is_rejected_with_no_row(
    service: CapabilityRequestService, store: SqliteStore
) -> None:
    forged = _envelope(from_agent_id="lane:swarm.worker.coding",
                       from_lane="lane:swarm.worker.coding")
    with pytest.raises(EnvelopeRejected) as rejected:
        service.submit(forged, channel_principal=WORKER)

    assert rejected.value.reason_code == "identity_mismatch"
    assert rejected.value.subject == "lane:swarm.worker.coding"
    assert store._connection.execute(
        "SELECT COUNT(*) AS n FROM capability_requests"
    ).fetchone()["n"] == 0

    # Nor can a worker launder itself into `system` or a human.
    for claimed in ("system", "human:owner"):
        with pytest.raises(EnvelopeRejected, match="identity_mismatch"):
            service.submit(
                _envelope(from_agent_id=claimed, from_lane=claimed), channel_principal=WORKER
            )
    assert store._connection.execute(
        "SELECT COUNT(*) AS n FROM capability_requests"
    ).fetchone()["n"] == 0


def test_counterfeit_an_unauthenticated_channel_cannot_submit(
    service: CapabilityRequestService,
) -> None:
    for principal in ("", "operator", "agent:bob", "system_impostor"):
        with pytest.raises(EnvelopeRejected) as rejected:
            service.submit(_envelope(), channel_principal=principal)
        assert rejected.value.reason_code == "non_canonical_principal"


def test_counterfeit_a_reused_request_id_cannot_smuggle_a_different_ask(
    service: CapabilityRequestService, store: SqliteStore
) -> None:
    """Idempotence must not become a rewrite channel."""
    envelope = _envelope(SkillRequest("copywriting.brief"))
    service.submit(envelope, channel_principal=WORKER)

    smuggled = _envelope(
        ToolRequest("stripe_acmeuni.read"), request_id=envelope.request_id
    )
    with pytest.raises(EnvelopeRejected) as rejected:
        service.submit(smuggled, channel_principal=WORKER)
    assert rejected.value.reason_code == "request_id_reuse"
    assert service.validation_events[-1] == {
        "event": "validation_rejected",
        "reason_code": "request_id_reuse",
        "subject": envelope.request_id,
    }

    stored = service.get(envelope.request_id)
    assert stored is not None
    assert stored["request_kind"] == "skill"
    assert _worker_grants(store) == []
