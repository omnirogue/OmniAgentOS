"""U-E2 — the exact floor, terminal CAS, the 90-second wall, and its telemetry.

The wall is tested with an INJECTED clock. A test that slept 90 seconds would be
testing ``time.sleep``, would add 90s to every suite run, and -- because the wall
is measured from the durable ``opened_at`` rather than from a stopwatch -- would
not even exercise the code path a restarted caller takes.
"""

from __future__ import annotations

import statistics
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.connectors import load_registry
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.provision.capability_policy import (
    AUTO_GRANT_TTL_SECONDS,
    CALLER_WALL_SECONDS,
    NEXT_ACTIONS,
    PAID_CAPABILITY_IDS,
    CapabilityDecisions,
    CapabilityPolicy,
    PolicyFacts,
    PolicyReceipt,
    PolicyViolation,
)

HOLDER = "lane:swarm.worker.research"
FLOOR_CAPABILITY = "echo.ping"


class FrozenClock:
    """A wall clock the test moves by hand."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    db_path = str(tmp_path / "policy.sqlite3")
    migrate(db_path)
    raw = SqliteStore(db_path)
    raw._connection.execute(
        "INSERT INTO agents (id, name, lineage, model, expertise_json, trust_level, "
        "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (HOLDER, HOLDER, "test.policy", None, "[]", "T1", "idle",
         "2026-08-03T00:00:00+00:00", "2026-08-03T00:00:00+00:00"),
    )
    raw._connection.commit()
    try:
        yield raw
    finally:
        raw.close()


def _decide(
    store: SqliteStore,
    policy: CapabilityPolicy,
    capability_id: str = FLOOR_CAPABILITY,
    *,
    request_id: str | None = None,
    in_approved_scope: bool = True,
    lane_allowed: bool = True,
    **kwargs: object,
) -> tuple[str, PolicyReceipt]:
    rid = request_id or str(uuid.uuid4())
    receipt = policy.decide(
        store,
        rid,
        holder_agent_id=HOLDER,
        subject_id=capability_id,
        in_approved_scope=in_approved_scope,
        lane_allowed=lane_allowed,
        **kwargs,  # type: ignore[arg-type]
    )
    return rid, receipt


def _grants(store: SqliteStore, agent_id: str = HOLDER) -> list[str]:
    rows = store._connection.execute(
        "SELECT capability_id FROM agent_capabilities WHERE agent_id = ? ORDER BY capability_id",
        (agent_id,),
    ).fetchall()
    return [str(row["capability_id"]) for row in rows]


# --------------------------------------------------------------- the predicate


def test_floor_predicate_is_the_exact_eight_way_conjunction() -> None:
    passing = PolicyFacts(
        known=True,
        callable_now=True,
        read_only=True,
        in_approved_scope=True,
        lane_allowed=True,
        paid=False,
        comms_write=False,
        danger_group=False,
    )
    assert passing.floor_allows is True
    assert passing.blocking_conjuncts == ()

    from dataclasses import replace

    for field, value, expected in [
        ("known", False, "known"),
        ("callable_now", False, "callable_now"),
        ("read_only", False, "read_only"),
        ("in_approved_scope", False, "in_approved_scope"),
        ("lane_allowed", False, "lane_allowed"),
        ("paid", True, "paid"),
        ("comms_write", True, "comms_write"),
        ("danger_group", True, "danger_group"),
    ]:
        broken = replace(passing, **{field: value})
        assert broken.floor_allows is False, f"{field} must be able to refuse the floor"
        assert broken.blocking_conjuncts == (expected,)


def test_paid_ledger_covers_the_loop_seam() -> None:
    """The policy's cost class may never be narrower than the budget ledger's.

    Two ledgers is the risk this pins: a capability the loop pre-pays for but the
    floor thinks is free would be auto-granted and then billed.
    """
    from omniagentos.scheduler.loop_effects import PAID_CAPABILITIES

    assert PAID_CAPABILITIES <= PAID_CAPABILITY_IDS


# ------------------------------------------------------------ decisive: floor


def test_floor_autogrants_echo_with_a_read_grant_and_a_1800s_ttl(store: SqliteStore) -> None:
    clock = FrozenClock()
    policy = CapabilityPolicy(clock=clock)
    request_id, receipt = _decide(store, policy)

    assert receipt.state == "granted"
    assert receipt.next_action == "retry_with_grant"
    assert receipt.granted_capability_ids == (FLOOR_CAPABILITY,)
    assert receipt.grant_decision_ms is not None

    row = store._connection.execute(
        "SELECT * FROM agent_capabilities WHERE agent_id = ?", (HOLDER,)
    ).fetchone()
    assert row["capability_id"] == FLOOR_CAPABILITY
    assert row["mode"] == "read"
    assert row["granted_by"] == "autogrant"
    assert row["issued_by"] == "autogrant"
    assert row["request_id"] == request_id
    expected_expiry = clock.now() + timedelta(seconds=AUTO_GRANT_TTL_SECONDS)
    assert datetime.fromisoformat(row["expires_at"]) == expected_expiry

    log = store._connection.execute(
        "SELECT * FROM capability_grant_log WHERE request_id = ?", (request_id,)
    ).fetchall()
    assert [entry["action"] for entry in log] == ["grant"]
    assert log[0]["actor"] == "autogrant"
    assert log[0]["mode"] == "read"


def test_autogrant_writes_the_same_columns_as_issue_scoped_grant(store: SqliteStore) -> None:
    """The hand-written grant INSERT must stay column-identical to the store's.

    ``cas_grant`` mirrors ``CapabilityStore.issue_scoped_grant`` instead of calling
    it (a nested BEGIN is an error). This is what stops the two from drifting.
    """
    from omniagentos.connectors.store import CapabilityStore

    _decide(store, CapabilityPolicy())
    auto = dict(
        store._connection.execute(
            "SELECT * FROM agent_capabilities WHERE agent_id = ?", (HOLDER,)
        ).fetchone()
    )

    other = "lane:runner.step"
    store._connection.execute(
        "INSERT INTO agents (id, name, lineage, model, expertise_json, trust_level, "
        "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (other, other, "test.policy", None, "[]", "T1", "idle",
         "2026-08-03T00:00:00+00:00", "2026-08-03T00:00:00+00:00"),
    )
    store._connection.commit()
    CapabilityStore(store).issue_scoped_grant(
        other,
        [FLOOR_CAPABILITY],
        mode="read",
        expires_at="2030-01-01T00:00:00+00:00",
        issued_by="autogrant",
        request_id=str(uuid.uuid4()),
        actor="autogrant",
    )
    manual = dict(
        store._connection.execute(
            "SELECT * FROM agent_capabilities WHERE agent_id = ?", (other,)
        ).fetchone()
    )
    assert sorted(auto) == sorted(manual)
    for column in ("mode", "granted_by", "issued_by"):
        assert auto[column] == manual[column]


def test_floor_decision_latency_meets_the_slo(store: SqliteStore) -> None:
    policy = CapabilityPolicy()
    samples: list[float] = []
    for _ in range(100):
        started = time.perf_counter()
        _, receipt = _decide(store, policy)
        samples.append((time.perf_counter() - started) * 1000)
        assert receipt.state == "granted"

    samples.sort()
    p50 = statistics.median(samples)
    p95 = samples[94]
    assert p50 < 100, f"floor p50 {p50:.1f}ms exceeds the 100ms SLO"
    assert p95 < 500, f"floor p95 {p95:.1f}ms exceeds the 500ms SLO"

    recorded = [
        int(row["grant_decision_ms"])
        for row in store._connection.execute(
            "SELECT grant_decision_ms FROM capability_decisions WHERE granted_by = 'autogrant'"
        ).fetchall()
    ]
    assert len(recorded) == 100
    assert statistics.median(recorded) < 100


# ------------------------------------------------ decisive: park then 90s wall


def test_a_paid_request_parks_and_then_cas_times_out_at_ninety_seconds(
    store: SqliteStore,
) -> None:
    clock = FrozenClock()
    # An operator declares echo.ping metered. Nothing else about it changes: it
    # is still callable, still read-only, still in scope. Only ¬paid refuses.
    policy = CapabilityPolicy(clock=clock, paid_capability_ids=frozenset({FLOOR_CAPABILITY}))
    request_id, parked = _decide(store, policy)

    assert parked.state == "pending_operator"
    assert parked.next_action == "await_operator"
    assert parked.terminal is False
    assert parked.facts[0][1].blocking_conjuncts == ("paid",)
    assert parked.reason_code == "requires_operator:paid"
    assert _grants(store) == []

    # One second before the wall the caller is still parked.
    clock.advance(CALLER_WALL_SECONDS - 1)
    assert policy.enforce_caller_wall(store, request_id).state == "pending_operator"

    clock.advance(1)
    timed_out = policy.enforce_caller_wall(store, request_id)
    assert timed_out.state == "timed_out"
    assert timed_out.terminal is True
    assert timed_out.reason_code == "caller_wall_90s"
    assert timed_out.next_action == "escalate_tier"
    assert timed_out.next_action in NEXT_ACTIONS
    assert timed_out.next_action != "await_operator"
    assert timed_out.grant_decision_ms == int(CALLER_WALL_SECONDS * 1000)
    assert _grants(store) == []

    # Idempotent: enforcing the wall again returns the same terminal receipt.
    assert policy.enforce_caller_wall(store, request_id).decided_at == timed_out.decided_at


def test_every_terminal_state_carries_an_actionable_next_move(store: SqliteStore) -> None:
    """The balance rule: no refusal may leave a worker with nothing to do."""
    clock = FrozenClock()
    policy = CapabilityPolicy(clock=clock)

    _, unknown = _decide(store, policy, "no_such_connector.read")
    _, dead_end = _decide(store, policy, "knowledge.read")  # declared, no http spec
    paid_policy = CapabilityPolicy(clock=clock, paid_capability_ids=frozenset({FLOOR_CAPABILITY}))
    parked_id, _ = _decide(store, paid_policy)
    clock.advance(CALLER_WALL_SECONDS)
    timed_out = paid_policy.enforce_caller_wall(store, parked_id)

    assert (unknown.state, unknown.reason_code, unknown.next_action) == (
        "hard_rejected",
        "unknown_capability",
        "stop_terminal",
    )
    assert (dead_end.state, dead_end.reason_code, dead_end.next_action) == (
        "hard_rejected",
        "no_call_path",
        "continue_degraded",
    )
    assert timed_out.next_action == "escalate_tier"
    for row in store._connection.execute("SELECT * FROM capability_decisions").fetchall():
        assert row["next_action"] in NEXT_ACTIONS
        if row["state"] != "pending_operator":
            assert row["next_action"] != "await_operator"


def test_a_replayed_request_id_returns_the_stored_decision(store: SqliteStore) -> None:
    policy = CapabilityPolicy()
    request_id, first = _decide(store, policy)
    _, second = _decide(store, policy, request_id=request_id)

    assert second.state == first.state == "granted"
    assert second.decided_at == first.decided_at
    assert (
        store._connection.execute("SELECT COUNT(*) AS n FROM capability_decisions").fetchone()["n"]
        == 1
    )


# ------------------------------------------------------------- COUNTERFEIT #1
#
# "Marking a paid capability auto-grantable cannot produce an active grant."


class _ForgedFloor(CapabilityPolicy):
    """A policy whose floor inputs have been tampered with to say yes.

    This is the realistic shape of the attack: the conjuncts are fed by a rung
    file, a lane allowlist, and a cost-class declaration, so the way a metered
    capability reaches the floor is by something upstream claiming it is free --
    not by anyone editing the predicate.
    """

    def facts_for(
        self,
        capability_id: str,
        *,
        in_approved_scope: bool,
        lane_allowed: bool,
    ) -> tuple[object, PolicyFacts]:  # type: ignore[override]
        capability = self._registry_loader().capability(capability_id)
        return capability, PolicyFacts(
            known=True,
            callable_now=True,
            read_only=True,
            in_approved_scope=True,
            lane_allowed=True,
            paid=False,
            comms_write=False,
            danger_group=False,
        )


def test_counterfeit_forged_floor_cannot_grant_a_paid_capability(
    store: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omniagentos.connectors import broker

    def resolution_is_a_counterfeit(*args: object, **kwargs: object) -> object:
        raise AssertionError("policy must never resolve a credential")

    monkeypatch.setattr(broker, "resolve_for", resolution_is_a_counterfeit)

    forged = _ForgedFloor(paid_capability_ids=frozenset({FLOOR_CAPABILITY}))
    request_id, receipt = _decide(store, forged)

    assert receipt.state == "denied"
    assert receipt.reason_code == "policy_violation"
    assert receipt.next_action == "continue_degraded"
    assert _grants(store) == []
    assert (
        store._connection.execute(
            "SELECT COUNT(*) AS n FROM capability_grant_log WHERE request_id = ?", (request_id,)
        ).fetchone()["n"]
        == 0
    )
    row = store._connection.execute(
        "SELECT * FROM capability_decisions WHERE request_id = ?", (request_id,)
    ).fetchone()
    assert row["state"] == "denied"
    assert row["decided_at"] is not None
    assert row["granted_by"] is None


def test_counterfeit_forged_floor_cannot_grant_a_write_or_a_danger_group(
    store: SqliteStore,
) -> None:
    forged = _ForgedFloor()
    _, write_receipt = _decide(store, forged, "knowledge.write")
    _, danger_receipt = _decide(store, forged, "stripe_acmeuni.read")

    # The forged facts carry BOTH past the early gates, so both land on the
    # independent re-read -- which is exactly the surface under test.
    assert (write_receipt.state, write_receipt.reason_code) == (
        "denied",
        "policy_violation",
    )
    assert (danger_receipt.state, danger_receipt.reason_code) == (
        "denied",
        "policy_violation",
    )
    assert _grants(store) == []


# ------------------------------------------------------------- COUNTERFEIT #2
#
# "A late operator grant racing the timeout LOSES the CAS and creates no grant."


def test_counterfeit_late_operator_grant_loses_the_cas(store: SqliteStore) -> None:
    clock = FrozenClock()
    policy = CapabilityPolicy(clock=clock, paid_capability_ids=frozenset({FLOOR_CAPABILITY}))
    request_id, _ = _decide(store, policy)

    clock.advance(CALLER_WALL_SECONDS + 5)
    timed_out = policy.enforce_caller_wall(store, request_id)
    assert timed_out.state == "timed_out"

    late = policy.operator_decide(
        store,
        request_id,
        decision="granted",
        operator="human:owner",
        capability_ids=[FLOOR_CAPABILITY],
    )
    assert late.state == "timed_out"
    assert late.granted is False
    assert late.decided_at == timed_out.decided_at
    assert _grants(store) == []

    # The approval is not lost -- it mints a FRESH request, which grants cleanly.
    fresh_id = str(uuid.uuid4())
    CapabilityDecisions(store).open(
        fresh_id,
        holder_agent_id=HOLDER,
        subject_kind="tool",
        subject_id=FLOOR_CAPABILITY,
        opened_at=timed_out.decided_at or "2026-08-03T12:00:00+00:00",
    )
    minted = policy.operator_decide(
        store,
        fresh_id,
        decision="granted",
        operator="human:owner",
        capability_ids=[FLOOR_CAPABILITY],
    )
    assert minted.state == "granted"
    assert _grants(store) == [FLOOR_CAPABILITY]
    row = store._connection.execute(
        "SELECT * FROM agent_capabilities WHERE agent_id = ?", (HOLDER,)
    ).fetchone()
    assert row["request_id"] == fresh_id
    assert row["granted_by"] == "human:owner"


def test_counterfeit_timeout_racing_a_won_operator_grant_loses(store: SqliteStore) -> None:
    """The race, run the other way: the wall must not overwrite a real grant."""
    clock = FrozenClock()
    policy = CapabilityPolicy(clock=clock, paid_capability_ids=frozenset({FLOOR_CAPABILITY}))
    request_id, _ = _decide(store, policy)

    granted = policy.operator_decide(
        store,
        request_id,
        decision="granted",
        operator="human:owner",
        capability_ids=[FLOOR_CAPABILITY],
    )
    assert granted.state == "granted"

    clock.advance(CALLER_WALL_SECONDS + 60)
    assert policy.enforce_caller_wall(store, request_id).state == "granted"
    assert _grants(store) == [FLOOR_CAPABILITY]


def test_a_terminal_row_cannot_be_rewritten_even_without_the_cas_where_clause(
    store: SqliteStore,
) -> None:
    """Migration 112's trigger is the second half of the CAS."""
    import sqlite3

    policy = CapabilityPolicy()
    request_id, _ = _decide(store, policy)
    with pytest.raises(sqlite3.IntegrityError, match="terminal"):
        store._connection.execute(
            "UPDATE capability_decisions SET state = 'denied' WHERE request_id = ?",
            (request_id,),
        )


# ------------------------------------------------------------- COUNTERFEIT #3
#
# "A READ_ONLY capability that is NOT callable_now is refused no_call_path,
#  never auto-granted into a dead end."


def test_counterfeit_read_only_without_a_call_path_is_refused_not_granted(
    store: SqliteStore,
) -> None:
    capability = load_registry().capability("knowledge.read")
    assert capability.resolved_read_only is True
    assert capability.callable_now is False

    policy = CapabilityPolicy()
    request_id, receipt = _decide(store, policy, "knowledge.read")

    assert receipt.state == "hard_rejected"
    assert receipt.reason_code == "no_call_path"
    assert receipt.next_action == "continue_degraded"
    assert receipt.excluded_capability_ids == ("knowledge.read",)
    assert _grants(store) == []

    # And it can never be talked into a grant afterwards: the row is terminal.
    with pytest.raises(PolicyViolation):
        policy._assert_floor_grantable(["knowledge.read"])
    assert (
        policy.operator_decide(
            store, request_id, decision="granted", operator="human:owner"
        ).state
        == "hard_rejected"
    )
    assert _grants(store) == []


# --------------------------------------------------------------- identity CHECK


@pytest.mark.parametrize(
    "identity",
    ["agent:bob", "system_impostor", "lane:", "human:", "lane:a:b", "Lane:x", "", "operator"],
)
def test_non_canonical_holders_cannot_open_a_decision(
    store: SqliteStore, identity: str
) -> None:
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        CapabilityDecisions(store).open(
            str(uuid.uuid4()),
            holder_agent_id=identity,
            subject_kind="tool",
            subject_id=FLOOR_CAPABILITY,
            opened_at="2026-08-03T12:00:00+00:00",
        )


@pytest.mark.parametrize(
    "identity",
    ["system", "lane:runner.step", "lane:swarm.worker.coding", "loop:w2_inbox_triage",
     "job:learnings-digest", "human:owner"],
)
def test_canonical_holders_are_accepted(store: SqliteStore, identity: str) -> None:
    row = CapabilityDecisions(store).open(
        str(uuid.uuid4()),
        holder_agent_id=identity,
        subject_kind="tool",
        subject_id=FLOOR_CAPABILITY,
        opened_at="2026-08-03T12:00:00+00:00",
    )
    assert row["holder_agent_id"] == identity


def test_only_the_floor_or_a_named_human_can_appear_as_granted_by(store: SqliteStore) -> None:
    import sqlite3

    request_id = str(uuid.uuid4())
    CapabilityDecisions(store).open(
        request_id,
        holder_agent_id=HOLDER,
        subject_kind="tool",
        subject_id=FLOOR_CAPABILITY,
        opened_at="2026-08-03T12:00:00+00:00",
    )
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "UPDATE capability_decisions SET state = 'granted', granted_by = 'cascade.yaml', "
            "expires_at = '2030-01-01T00:00:00+00:00', granted_ids_json = '[\"echo.ping\"]', "
            "decided_at = '2026-08-03T12:00:00+00:00', grant_decision_ms = 1, "
            "next_action = 'retry_with_grant' WHERE request_id = ?",
            (request_id,),
        )


def test_an_operator_decision_must_name_a_human(store: SqliteStore) -> None:
    policy = CapabilityPolicy(paid_capability_ids=frozenset({FLOOR_CAPABILITY}))
    request_id, _ = _decide(store, policy)
    for spelling in ["operator", "owner", "human:", "system"]:
        with pytest.raises(ValueError, match="human:"):
            policy.operator_decide(
                store, request_id, decision="granted", operator=spelling
            )
    assert _grants(store) == []
