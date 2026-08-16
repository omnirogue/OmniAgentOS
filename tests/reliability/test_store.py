"""Comprehensive tests for SqliteReliabilityStore (W1 contract).

Tests cover durability & concurrency contracts per §5b:
  1. CAS transitions (conflicting concurrent updates)
  2. Lease fencing (mutual exclusion, stale reclaim)
  3. Hash chain verification (immutability)
  4. Occurrence key idempotency (ON CONFLICT IGNORE)
  5. Vote idempotency (UNIQUE + REPLACE)
  6. Autonomy scope resolution (most-specific wins)
  7. SQLITE_BUSY retry policy (smoke test)
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.reliability.contracts import (
    LeaseConflict,
    TransitionConflict,
)
from omniagentos.reliability.store import _GENESIS_HASH, SqliteReliabilityStore, _hash_log_row
from omniagentos.reliability.taxonomy import (
    AutonomyMode,
    EventStatus,
    FailureClass,
    ImprovementStatus,
    Severity,
)
from tests.support.db_template import make_store


@pytest.fixture
def tmp_db():
    """Create a temporary SQLite database and run migrations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Template copy carries the migrated schema; the store constructor
        # still runs migrate_connection over it (now a no-op re-verify).
        store = make_store(SqliteReliabilityStore, Path(tmpdir) / "test.db")
        yield store
        # Cleanup: close connection
        if hasattr(store, "_connection"):
            store._connection.close()


# --- CAS Transition Tests


def test_cas_transition_success(tmp_db):
    """CAS transition should succeed when expected status matches."""
    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Test improvement",
    )

    # Transition: proposed → testing
    tmp_db.transition_improvement(
        imp_id,
        ImprovementStatus.PROPOSED.value,
        ImprovementStatus.TESTING.value,
        actor="pipeline",
    )

    imp = tmp_db.get_improvement(imp_id)
    assert imp is not None
    assert imp.status == ImprovementStatus.TESTING.value
    assert imp.version == 1


def test_cas_transition_conflict_status_mismatch(tmp_db):
    """CAS transition should fail if expected status doesn't match."""
    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Test improvement",
    )

    # Try to transition from wrong status
    with pytest.raises(TransitionConflict):
        tmp_db.transition_improvement(
            imp_id,
            ImprovementStatus.TESTING.value,  # Wrong: it's PROPOSED
            ImprovementStatus.JUDGING.value,
            actor="pipeline",
        )


def test_cas_transition_concurrent_race(tmp_db):
    """CAS transitions are atomic: simulated concurrency via sequential checks."""
    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Test improvement",
    )

    # First transition: proposed → testing (should succeed)
    tmp_db.transition_improvement(
        imp_id,
        ImprovementStatus.PROPOSED.value,
        ImprovementStatus.TESTING.value,
        actor="pipeline-1",
    )

    imp = tmp_db.get_improvement(imp_id)
    assert imp.version == 1
    assert imp.status == ImprovementStatus.TESTING.value

    # Second transition from wrong status should fail
    with pytest.raises(TransitionConflict):
        tmp_db.transition_improvement(
            imp_id,
            ImprovementStatus.PROPOSED.value,  # Wrong: already TESTING
            ImprovementStatus.JUDGING.value,
            actor="pipeline-2",
        )

    # Status should not have changed
    imp = tmp_db.get_improvement(imp_id)
    assert imp.version == 1
    assert imp.status == ImprovementStatus.TESTING.value

    # Third transition with correct status should succeed
    tmp_db.transition_improvement(
        imp_id,
        ImprovementStatus.TESTING.value,
        ImprovementStatus.JUDGING.value,
        actor="pipeline-2",
    )

    imp = tmp_db.get_improvement(imp_id)
    assert imp.version == 2
    assert imp.status == ImprovementStatus.JUDGING.value


def test_cas_transition_appends_log_row(tmp_db):
    """Each CAS transition should append an immutable log row."""
    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Test improvement",
    )

    tmp_db.transition_improvement(
        imp_id,
        ImprovementStatus.PROPOSED.value,
        ImprovementStatus.TESTING.value,
        actor="test-actor",
        detail_json={"reason": "test"},
    )

    # Verify log row was appended
    conn = tmp_db._connection
    log_rows = conn.execute(
        "SELECT * FROM reliability_log WHERE entity_type = ? AND entity_id = ? ORDER BY id",
        ("improvement", imp_id),
    ).fetchall()

    assert len(log_rows) == 1
    row = log_rows[0]
    assert row["from_status"] == ImprovementStatus.PROPOSED.value
    assert row["to_status"] == ImprovementStatus.TESTING.value
    assert row["actor"] == "test-actor"
    assert json.loads(row["detail_json"]) == {"reason": "test"}
    assert row["prev_hash"] == _GENESIS_HASH
    assert row["hash"] is not None


# --- Hash Chain Tests


def test_hash_chain_genesis(tmp_db):
    """First log entry should use genesis hash as prev_hash."""
    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Test",
    )

    tmp_db.transition_improvement(
        imp_id,
        ImprovementStatus.PROPOSED.value,
        ImprovementStatus.TESTING.value,
        actor="actor1",
    )

    conn = tmp_db._connection
    row = conn.execute("SELECT prev_hash, hash FROM reliability_log ORDER BY id LIMIT 1").fetchone()

    assert row["prev_hash"] == _GENESIS_HASH
    assert len(row["hash"]) == 64  # SHA256 hex


def test_hash_chain_continuity(tmp_db):
    """Each log row should chain from previous hash."""
    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Test",
    )

    # Create 3 transitions
    tmp_db.transition_improvement(
        imp_id,
        ImprovementStatus.PROPOSED.value,
        ImprovementStatus.TESTING.value,
        actor="actor1",
    )

    tmp_db.transition_improvement(
        imp_id,
        ImprovementStatus.TESTING.value,
        ImprovementStatus.JUDGING.value,
        actor="actor2",
    )

    tmp_db.transition_improvement(
        imp_id,
        ImprovementStatus.JUDGING.value,
        ImprovementStatus.APPROVED.value,
        actor="actor3",
    )

    conn = tmp_db._connection
    rows = conn.execute("SELECT prev_hash, hash FROM reliability_log ORDER BY id").fetchall()

    assert len(rows) == 3

    # First row: prev=genesis
    assert rows[0]["prev_hash"] == _GENESIS_HASH

    # Chain: each prev_hash = previous hash
    assert rows[1]["prev_hash"] == rows[0]["hash"]
    assert rows[2]["prev_hash"] == rows[1]["hash"]


def test_hash_chain_verification(tmp_db):
    """Hash chain should be verifiable end-to-end."""
    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Test",
    )

    tmp_db.transition_improvement(
        imp_id,
        ImprovementStatus.PROPOSED.value,
        ImprovementStatus.TESTING.value,
        actor="actor1",
    )

    conn = tmp_db._connection
    row = conn.execute(
        "SELECT prev_hash, entity_type, entity_id, from_status, to_status, actor, detail_json, ts, hash "
        "FROM reliability_log WHERE id = 1"
    ).fetchone()

    # Reconstruct the hash
    row_dict = {
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "from_status": row["from_status"],
        "to_status": row["to_status"],
        "actor": row["actor"],
        "detail_json": json.loads(row["detail_json"]),
        "ts": row["ts"],
    }

    computed_hash = _hash_log_row(row["prev_hash"], row_dict)
    assert computed_hash == row["hash"]


# --- Occurrence Key Idempotency Tests


def test_occurrence_key_idempotency(tmp_db):
    """Duplicate events with same occurrence_key should be ignored (ON CONFLICT IGNORE)."""
    # Insert first event
    tmp_db.insert_reliability_event(
        failure_class=FailureClass.RUN_FAILED.value,
        severity=Severity.WARNING.value,
        signature="sig1",
        occurrence_key="occ_key_1",
        source="detector1",
    )

    # Try to insert duplicate with same occurrence_key
    tmp_db.insert_reliability_event(
        failure_class=FailureClass.RUN_FAILED.value,
        severity=Severity.CRITICAL.value,  # Different severity
        signature="sig1_different",  # Different signature
        occurrence_key="occ_key_1",  # Same occurrence_key
        source="detector2",  # Different source
    )

    # Both calls should succeed (idempotent), but only one row inserted
    events = tmp_db.list_events(limit=100)
    assert len(events) == 1

    event = events[0]
    assert event.severity == Severity.WARNING.value  # First insert wins
    assert event.signature == "sig1"  # First insert wins


# --- Vote Idempotency Tests


def test_vote_idempotency(tmp_db):
    """Duplicate votes for the same seat are append-only no-ops: the first vote stands."""
    imp_id = tmp_db.create_improvement(
        origin="realtime",
        kind="fix",
        title="Test",
    )

    # Insert first vote
    vote1_id = tmp_db.insert_vote(
        improvement_id=imp_id,
        panel_attempt_id="pan_1",
        judge_agent="judge_a",
        model_family="anthropic",
        verdict="approve",
        reasoning="Looks good",
    )

    # Try to insert vote with same (improvement_id, panel_attempt_id, model_family)
    vote2_id = tmp_db.insert_vote(
        improvement_id=imp_id,
        panel_attempt_id="pan_1",
        judge_agent="judge_a_retry",  # Different judge
        model_family="anthropic",  # Same family
        verdict="reject",  # Different verdict
        reasoning="On second thought, no",
    )

    # Fetch votes for this improvement
    votes = tmp_db.list_votes(improvement_id=imp_id)

    # Append-only (§6 m3, codex #16): exactly one vote, and the FIRST stands —
    # a retried judge seat can never erase or overwrite a recorded verdict.
    assert len(votes) == 1
    assert vote2_id == vote1_id  # retry returns the existing vote id

    vote = votes[0]
    assert vote.verdict == "approve"
    assert vote.reasoning == "Looks good"


# --- Lease Tests


def test_lease_acquire_success(tmp_db):
    """Should acquire a new lease."""
    token = tmp_db.acquire_lease(
        key="test_lease",
        owner="owner1",
        duration_seconds=3600,
    )

    assert token is not None
    assert len(token) > 0


def test_lease_renew_same_owner(tmp_db):
    """Same owner should renew their lease."""
    token1 = tmp_db.acquire_lease(
        key="test_lease",
        owner="owner1",
        duration_seconds=3600,
    )

    # Renew (same owner)
    token2 = tmp_db.acquire_lease(
        key="test_lease",
        owner="owner1",
        duration_seconds=3600,
    )

    # Token should be refreshed
    assert token2 is not None
    assert token2 != token1  # New token


def test_lease_conflict_different_owner(tmp_db):
    """Different owner should fail if lease still valid."""
    tmp_db.acquire_lease(
        key="test_lease",
        owner="owner1",
        duration_seconds=3600,
    )

    # Try to acquire with different owner
    with pytest.raises(LeaseConflict):
        tmp_db.acquire_lease(
            key="test_lease",
            owner="owner2",
            duration_seconds=3600,
        )


def test_lease_fencing_token_validation(tmp_db):
    """Renew should validate token."""
    tmp_db.acquire_lease(
        key="test_lease",
        owner="owner1",
        duration_seconds=3600,
    )

    # Try to renew with wrong token
    with pytest.raises(LeaseConflict):
        tmp_db.renew_lease(
            key="test_lease",
            owner="owner1",
            token="wrong_token",
            duration_seconds=3600,
        )


def test_displaced_lease_owner_cannot_pass_the_mutation_fence(tmp_db):
    """A reclaimed token is rejected before its former owner mutates."""
    token1 = tmp_db.acquire_lease(
        key="test_lease",
        owner="owner1",
        duration_seconds=3600,
    )
    row = tmp_db._connection.execute(
        "SELECT value_json FROM reliability_state WHERE key = ?",
        ("lease:test_lease",),
    ).fetchone()
    lease = json.loads(row["value_json"])
    lease["expires_at"] = "2026-01-01T00:00:00Z"
    tmp_db._connection.execute(
        "UPDATE reliability_state SET value_json = ? WHERE key = ?",
        (json.dumps(lease), "lease:test_lease"),
    )

    token2 = tmp_db.acquire_lease(
        key="test_lease",
        owner="owner2",
        duration_seconds=3600,
    )

    with pytest.raises(LeaseConflict):
        tmp_db.assert_lease("test_lease", "owner1", token1)
    tmp_db.assert_lease("test_lease", "owner2", token2)


def test_corrupt_lease_generation_fails_closed(tmp_db):
    tmp_db._connection.execute(
        """
        INSERT INTO reliability_state (key, value_json, updated_at)
        VALUES (?, ?, ?)
        """,
        (
            "lease:test_lease",
            json.dumps(
                {
                    "owner": "owner1",
                    "token": "token1",
                    "generation": "not-an-integer",
                    "expires_at": "2099-01-01T00:00:00Z",
                }
            ),
            utc_now_iso(),
        ),
    )

    with pytest.raises(LeaseConflict, match="state is corrupt"):
        tmp_db.acquire_lease(
            key="test_lease",
            owner="owner1",
            duration_seconds=3600,
        )


def test_lease_release(tmp_db):
    """Release should allow re-acquisition."""
    token1 = tmp_db.acquire_lease(
        key="test_lease",
        owner="owner1",
        duration_seconds=3600,
    )

    tmp_db.release_lease(
        key="test_lease",
        owner="owner1",
        token=token1,
    )

    # Now owner2 should be able to acquire
    token2 = tmp_db.acquire_lease(
        key="test_lease",
        owner="owner2",
        duration_seconds=3600,
    )

    assert token2 is not None


def test_lease_stale_reclaim(tmp_db):
    """Should reclaim expired lease."""
    # Acquire and set to expired by directly manipulating state
    tmp_db.acquire_lease(
        key="test_lease",
        owner="owner1",
        duration_seconds=1,  # Very short
    )

    # Wait for expiry
    import time

    time.sleep(1.1)

    # Reclaim should succeed
    reclaimed = tmp_db.reclaim_stale_lease(
        key="test_lease",
        threshold_seconds=0,  # No threshold
    )

    assert reclaimed is True

    # Now a new owner should be able to acquire
    token = tmp_db.acquire_lease(
        key="test_lease",
        owner="owner2",
        duration_seconds=3600,
    )
    assert token is not None


def test_lease_stale_reclaim_honors_threshold_after_expiry(tmp_db):
    """Expiry alone is insufficient until the requested stale grace elapses."""
    tmp_db.acquire_lease(
        key="test_lease",
        owner="owner1",
        duration_seconds=3600,
    )
    row = tmp_db._connection.execute(
        "SELECT value_json FROM reliability_state WHERE key = ?",
        ("lease:test_lease",),
    ).fetchone()
    lease = json.loads(row["value_json"])
    lease["expires_at"] = (datetime.now(UTC) - timedelta(seconds=120)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    tmp_db._connection.execute(
        "UPDATE reliability_state SET value_json = ? WHERE key = ?",
        (json.dumps(lease), "lease:test_lease"),
    )

    assert tmp_db.reclaim_stale_lease("test_lease", threshold_seconds=300) is False
    assert tmp_db._connection.execute(
        "SELECT 1 FROM reliability_state WHERE key = ?",
        ("lease:test_lease",),
    ).fetchone()
    assert tmp_db.reclaim_stale_lease("test_lease", threshold_seconds=60) is True


# --- Autonomy Scope Resolution Tests


def test_autonomy_resolve_global_default(tmp_db):
    """Should return global default if no specific setting."""
    # Ensure global setting exists (seeded by migration)
    setting = tmp_db.resolve_autonomy()

    assert setting is not None
    assert setting.scope_type == "global"
    assert setting.mode == AutonomyMode.APPROVE.value
    assert setting.max_auto_risk == 0


def test_autonomy_agent_scope_wins(tmp_db):
    """Agent-specific autonomy should win over global."""
    # Create org_unit and agent
    org_id = tmp_db.create_org_unit(
        name="Engineering",
        kind="department",
    )

    agent_id = tmp_db.create_agent(
        name="test-agent",
        org_unit_id=org_id,
        org_role="specialist",
    )

    # Manually insert autonomy settings for testing
    conn = tmp_db._connection

    # Global: approve/0
    conn.execute(
        """
        INSERT OR IGNORE INTO autonomy_settings (id, scope_type, scope_id, mode, max_auto_risk, updated_by, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("aut_global", "global", "", AutonomyMode.APPROVE.value, 0, "test", utc_now_iso()),
    )

    # Agent: auto/2
    conn.execute(
        """
        INSERT INTO autonomy_settings (id, scope_type, scope_id, mode, max_auto_risk, updated_by, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("aut_agent", "agent", agent_id, AutonomyMode.AUTO.value, 2, "test", utc_now_iso()),
    )
    conn.commit()

    # Resolve: agent scope should win
    setting = tmp_db.resolve_autonomy(agent_id=agent_id)

    assert setting.scope_type == "agent"
    assert setting.mode == AutonomyMode.AUTO.value
    assert setting.max_auto_risk == 2


def test_autonomy_department_scope(tmp_db):
    """Department autonomy should win over global."""
    # Create org_unit
    org_id = tmp_db.create_org_unit(
        name="Research",
        kind="department",
    )

    agent_id = tmp_db.create_agent(
        name="test-agent",
        org_unit_id=org_id,
        org_role="specialist",
    )

    conn = tmp_db._connection

    # Global: approve/0
    conn.execute(
        """
        INSERT OR IGNORE INTO autonomy_settings (id, scope_type, scope_id, mode, max_auto_risk, updated_by, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("aut_global", "global", "", AutonomyMode.APPROVE.value, 0, "test", utc_now_iso()),
    )

    # Department: auto/1
    conn.execute(
        """
        INSERT INTO autonomy_settings (id, scope_type, scope_id, mode, max_auto_risk, updated_by, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("aut_dept", "department", org_id, AutonomyMode.AUTO.value, 1, "test", utc_now_iso()),
    )
    conn.commit()

    # Resolve: department scope should win (no agent-specific setting)
    setting = tmp_db.resolve_autonomy(agent_id=agent_id)

    assert setting.scope_type == "department"
    assert setting.mode == AutonomyMode.AUTO.value
    assert setting.max_auto_risk == 1


# --- Event Lifecycle Tests


def test_event_claim_recovery_success(tmp_db):
    """claim_recovery should transition open → recovering."""
    evt_id = tmp_db.insert_reliability_event(
        failure_class=FailureClass.TIMEOUT.value,
        severity=Severity.WARNING.value,
        signature="sig1",
        occurrence_key="occ_1",
        source="detector1",
    )

    # Claim recovery
    claimed = tmp_db.claim_recovery(evt_id)
    assert claimed is True

    event = tmp_db.get_event(evt_id)
    assert event.status == EventStatus.RECOVERING.value


def test_event_claim_recovery_idempotent(tmp_db):
    """claim_recovery should fail if not in open status."""
    evt_id = tmp_db.insert_reliability_event(
        failure_class=FailureClass.TIMEOUT.value,
        severity=Severity.WARNING.value,
        signature="sig1",
        occurrence_key="occ_1",
        source="detector1",
    )

    # Claim once (succeeds)
    claimed1 = tmp_db.claim_recovery(evt_id)
    assert claimed1 is True

    # Claim again (fails; already recovering)
    claimed2 = tmp_db.claim_recovery(evt_id)
    assert claimed2 is False


# --- Audit Lifecycle Tests


def test_audit_lifecycle(tmp_db):
    """Audit should progress through states: queued → running → completed."""
    audit_id = tmp_db.create_audit(
        kind="watch",
        window_start=utc_now_iso(),
        window_end=utc_now_iso(),
    )

    audit = tmp_db.get_audit(audit_id)
    assert audit.status == "queued"

    # Start
    tmp_db.start_audit(audit_id)
    audit = tmp_db.get_audit(audit_id)
    assert audit.status == "running"

    # Complete
    tmp_db.complete_audit(
        audit_id,
        stats_json={"key": "value"},
        findings=5,
    )
    audit = tmp_db.get_audit(audit_id)
    assert audit.status == "completed"
    assert audit.findings == 5


# --- Watch Cursor Tests


def test_watch_cursor_advance(tmp_db):
    """Watch cursor should advance and persist."""
    cursor1 = tmp_db.get_watch_cursor()
    assert cursor1 is None

    never = tmp_db.get_watch_state()
    assert never == {
        "state": "never_run",
        "cursor_at": None,
        "heartbeat_at": None,
        "error": None,
    }

    new_cursor = "2026-07-24T12:00:00Z"
    tmp_db.advance_watch_cursor(new_cursor)

    cursor2 = tmp_db.get_watch_cursor()
    assert cursor2 == new_cursor
    recorded = tmp_db.get_watch_state()
    assert recorded["state"] == "recorded"
    assert recorded["cursor_at"] == new_cursor
    assert recorded["heartbeat_at"]


def test_watch_state_keeps_durable_timestamp_when_payload_is_corrupt(tmp_db):
    heartbeat_at = "2026-07-24T12:05:00Z"
    tmp_db._connection.execute(
        """
        INSERT INTO reliability_state (key, value_json, updated_at)
        VALUES ('watch_cursor', '{broken', ?)
        """,
        (heartbeat_at,),
    )

    state = tmp_db.get_watch_state()

    assert state == {
        "state": "corrupt",
        "cursor_at": None,
        "heartbeat_at": heartbeat_at,
        "error": "corrupt_watch_state",
    }


# --- Scorecard Tests


def test_scorecard_upsert(tmp_db):
    """Scorecard upsert should insert or replace."""
    # First insert
    tmp_db.upsert_scorecard(
        subject_type="agent",
        subject_id="agt_123",
        window="day",
        period_start="2026-07-23",
        metrics_json={"success_rate": 0.95},
    )

    sc = tmp_db.get_scorecard(
        subject_type="agent",
        subject_id="agt_123",
        window="day",
        period_start="2026-07-23",
    )
    assert sc is not None
    assert sc.metrics_json["success_rate"] == 0.95

    # Upsert (replace)
    tmp_db.upsert_scorecard(
        subject_type="agent",
        subject_id="agt_123",
        window="day",
        period_start="2026-07-23",
        metrics_json={"success_rate": 0.98},  # Updated
    )

    # Should still be one scorecard
    scorecards = tmp_db.list_scorecards(
        subject_type="agent",
        subject_id="agt_123",
    )
    assert len(scorecards) == 1
    assert scorecards[0].metrics_json["success_rate"] == 0.98


# --- Agent Tests


def test_agent_org_columns(tmp_db):
    """Agent should have org placement columns."""
    org_id = tmp_db.create_org_unit(
        name="Engineering",
        kind="department",
    )

    agent_id = tmp_db.create_agent(
        name="test-agent",
        org_unit_id=org_id,
        org_role="specialist",
        title="Software Engineer",
        charter="Build stuff",
        harness="cli-claude",
    )

    agent = tmp_db.get_agent(agent_id)
    assert agent.org_unit_id == org_id
    assert agent.org_role == "specialist"
    assert agent.title == "Software Engineer"
    assert agent.charter == "Build stuff"
    assert agent.harness == "cli-claude"


# --- Agent Request Tests


def test_agent_request_lifecycle(tmp_db):
    """Agent request should progress through states."""
    req_id = tmp_db.create_agent_request(
        description="Create a new agent for research",
    )

    req = tmp_db.get_agent_request(req_id)
    assert req.status == "pending"

    # Update to designing
    tmp_db.update_agent_request_status(
        req_id,
        status="designing",
        design_json={"name": "research-agent"},
    )

    req = tmp_db.get_agent_request(req_id)
    assert req.status == "designing"
    assert req.design_json["name"] == "research-agent"


def test_agent_request_attribution_round_trips_through_real_sqlite(tmp_db):
    """The four migration-105 columns survive a REAL create -> get -> list.

    The API-side tests for this feature run against a hand-written fake that
    re-implements create/get/list over a dict, so they could not see that the
    production readers called ``.get()`` on a ``sqlite3.Row`` — a type that has
    no ``.get()``. Every real read raised ``AttributeError``. This test is the
    evidence the fake could not be: a migrated database, the real store, and the
    attribution fields read back off actual rows.
    """
    req_id = tmp_db.create_agent_request(
        description="Wire the payouts reconciler",
        from_agent_id="lane:swarm.worker.coding",
        from_lane="swarm.worker.coding",
        session_id="ses_abc123",
        run_id="run_def456",
    )

    fetched = tmp_db.get_agent_request(req_id)
    assert fetched is not None
    assert fetched.from_agent_id == "lane:swarm.worker.coding"
    assert fetched.from_lane == "swarm.worker.coding"
    assert fetched.session_id == "ses_abc123"
    assert fetched.run_id == "run_def456"
    # Display attribution is the canonical identity, never the hardcoded operator.
    assert fetched.requested_by == "lane:swarm.worker.coding"
    assert fetched.requested_by != "owner"

    listed = [row for row in tmp_db.list_agent_requests() if row.id == req_id]
    assert len(listed) == 1
    assert listed[0].from_agent_id == "lane:swarm.worker.coding"
    assert listed[0].session_id == "ses_abc123"
    assert listed[0].run_id == "run_def456"


def test_agent_request_without_attribution_reads_back_as_system(tmp_db):
    """An internal event with no authenticated principal is 'system', not NULL."""
    req_id = tmp_db.create_agent_request(description="Internal maintenance request")

    fetched = tmp_db.get_agent_request(req_id)
    assert fetched is not None
    assert fetched.from_agent_id == "system"
    assert fetched.from_lane is None
    assert fetched.session_id is None
    assert fetched.run_id is None


def test_agent_request_refuses_non_canonical_identity(tmp_db):
    """The write path is the enforcement point; nothing non-canonical lands."""
    for spelling in ("agent:bob", "system_impostor", "lane:", "", "owner"):
        with pytest.raises(ValueError, match="Invalid from_agent_id"):
            tmp_db.create_agent_request(description="x", from_agent_id=spelling)
