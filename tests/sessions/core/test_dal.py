from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omniagentos.sessions.dal import (
    SessionsDal,
    decode_granted_roots,
    encode_granted_roots,
)

from .conftest import seed_session


def test_encode_decode_granted_roots_round_trip() -> None:
    assert encode_granted_roots(None) is None
    assert encode_granted_roots([]) is None
    assert encode_granted_roots(["  ", ""]) is None
    # Order-preserving + deduplicated.
    encoded = encode_granted_roots(["/a", "/b", "/a", " /c "])
    assert decode_granted_roots({"granted_roots": encoded}) == ["/a", "/b", "/c"]


def test_decode_granted_roots_fails_closed() -> None:
    assert decode_granted_roots(None) == []
    assert decode_granted_roots({}) == []
    assert decode_granted_roots({"granted_roots": None}) == []
    assert decode_granted_roots({"granted_roots": "not json"}) == []
    assert decode_granted_roots({"granted_roots": json.dumps({"a": 1})}) == []
    assert decode_granted_roots({"granted_roots": json.dumps(["/a", 3, "  "])}) == ["/a"]


def test_create_session_persists_granted_roots(sessions_dal: SessionsDal, tmp_path: Path) -> None:
    session_id = seed_session(sessions_dal, tmp_path, granted_roots=json.dumps(["/scope"]))
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert decode_granted_roots(row) == ["/scope"]


def test_get_claude_account_round_trip(sessions_dal: SessionsDal) -> None:
    """get_claude_account feeds the live-transcript config_dir resolution."""
    assert sessions_dal.get_claude_account("acct_missing") is None
    sessions_dal._connection.execute(
        "INSERT INTO claude_accounts (id, label, auth_type, config_dir, created_at, updated_at) "
        "VALUES ('acct_1', 'Main', 'config_dir', '/cfg/main', 'now', 'now')"
    )
    sessions_dal._connection.commit()
    row = sessions_dal.get_claude_account("acct_1")
    assert row is not None
    assert row["config_dir"] == "/cfg/main"


def test_create_list_and_legal_transition_guards(sessions_dal: SessionsDal, tmp_path: Path) -> None:
    session_id = seed_session(sessions_dal, tmp_path)
    assert sessions_dal.get_session(session_id)["state"] == "starting"  # type: ignore[index]
    assert sessions_dal.update_session_state(session_id, "running", expect="starting")
    assert not sessions_dal.update_session_state(session_id, "completed", expect="starting")
    assert sessions_dal.update_session_state(session_id, "awaiting_approval", expect=["running"])
    assert sessions_dal.update_session_state(session_id, "resuming", expect="awaiting_approval")
    assert sessions_dal.update_session_state(session_id, "completed", expect="resuming")
    assert not sessions_dal.update_session_state(session_id, "running")
    assert [row["id"] for row in sessions_dal.list_sessions("completed")] == [session_id]


def test_create_approval_is_idempotent_and_point_lookup(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = seed_session(sessions_dal, tmp_path)
    approval_id = sessions_dal.create_session_approval(
        session_id,
        "abc123",
        "consequential",
        "write outside project",
        '{"file_path":"/tmp/out"}',
        "high",
        "outside project",
        "2099-01-01T00:00:00Z",
    )
    duplicate = sessions_dal.create_session_approval(
        session_id,
        "abc123",
        "consequential",
        "write outside project",
        '{"file_path":"/tmp/out"}',
        "high",
        "outside project",
        "2099-01-01T00:00:00Z",
    )
    assert duplicate == approval_id
    row = sessions_dal.get_approval_by_id(approval_id)
    assert row is not None
    assert row["session_id"] == session_id
    assert row["run_id"] is None
    assert row["step_seq"] is None
    assert json.loads(row["params_json"])["action_hash"] == "abc123"


def test_create_approval_resolves_claude_session_ref_to_canonical_id(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = seed_session(sessions_dal, tmp_path)
    approval_id = sessions_dal.create_session_approval(
        "11111111-2222-3333-4444-555555555555",
        "hash",
        "consequential",
        "write",
        "{}",
        "high",
        "",
        None,
    )
    assert sessions_dal.get_approval_by_id(approval_id)["session_id"] == session_id  # type: ignore[index]


def test_has_pending_approval_tracks_only_pending_rows(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = seed_session(sessions_dal, tmp_path)
    assert not sessions_dal.has_pending_approval(session_id)
    approval_id = sessions_dal.create_session_approval(
        session_id, "hash", "irreversible", "risky", "{}", "high", "", None
    )
    assert sessions_dal.has_pending_approval(session_id)
    sessions_dal._connection.execute(  # noqa: SLF001 - arrange a decided approval
        "UPDATE approvals SET state = 'approved' WHERE id = ?", (approval_id,)
    )
    sessions_dal._connection.commit()  # noqa: SLF001
    assert not sessions_dal.has_pending_approval(session_id)


def test_void_only_this_sessions_pending_approvals(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    first = seed_session(sessions_dal, tmp_path, session_id="ses_first")
    second = seed_session(sessions_dal, tmp_path, session_id="ses_second")
    for session_id in (first, second):
        sessions_dal.create_session_approval(
            session_id, session_id, "consequential", "test", "{}", "", "", None
        )
    assert sessions_dal.void_session_approvals(first, "terminal") == 1
    assert sessions_dal.get_latest_session_approval(first)["state"] == "expired"  # type: ignore[index]
    assert sessions_dal.get_latest_session_approval(second)["state"] == "pending"  # type: ignore[index]


def test_activity_updates_are_coalesced_and_never_create_progress_events(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = seed_session(sessions_dal, tmp_path)
    assert sessions_dal.touch_activity(session_id, "2026-01-01T00:00:00Z")
    assert not sessions_dal.touch_activity(session_id, "2026-01-01T00:00:01Z")
    assert sessions_dal.get_session(session_id)["last_activity_at"] == "2026-01-01T00:00:00Z"  # type: ignore[index]
    rows = sessions_dal._connection.execute(  # noqa: SLF001 - verify the persisted audit seam
        "SELECT type, action FROM events WHERE target_id = ?", (session_id,)
    ).fetchall()
    # T-DESIGN-002/AC-15: lifecycle persists as an AUDIT row (never session.* / session.updated).
    assert [row["type"] for row in rows] == ["audit.event"]
    assert [row["action"] for row in rows] == ["session.created"]
    assert not any(row["type"] == "session.updated" for row in rows)


def test_lifecycle_transitions_persist_as_audit_not_session_types(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = seed_session(sessions_dal, tmp_path)
    assert sessions_dal.update_session_state(session_id, "running", expect="starting")
    assert sessions_dal.update_session_state(session_id, "awaiting_approval", expect="running")
    rows = sessions_dal._connection.execute(  # noqa: SLF001 - verify the persisted audit seam
        "SELECT type, action FROM events WHERE target_type = 'session' AND target_id = ? "
        "ORDER BY id ASC",
        (session_id,),
    ).fetchall()
    assert {row["type"] for row in rows} == {"audit.event"}
    assert [row["action"] for row in rows] == [
        "session.created",
        "session.running",
        "session.awaiting_approval",
    ]


def test_lifecycle_events_populate_execution_id_and_gap_free_sequence(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """W2.6 (086): a session IS the execution unit for this lane, so every
    ``_event`` row (create_session, update_session_state) must carry
    execution_id=session_id and a dense per-session 1, 2, 3, ... sequence."""
    session_id = seed_session(sessions_dal, tmp_path)
    assert sessions_dal.update_session_state(session_id, "running", expect="starting")
    assert sessions_dal.update_session_state(session_id, "awaiting_approval", expect="running")
    rows = sessions_dal._connection.execute(  # noqa: SLF001 - verify the persisted audit seam
        "SELECT execution_id, sequence FROM events WHERE target_type = 'session' "
        "AND target_id = ? ORDER BY id ASC",
        (session_id,),
    ).fetchall()
    assert [row["execution_id"] for row in rows] == [session_id] * 3
    assert [row["sequence"] for row in rows] == [1, 2, 3]


def test_record_session_error_populates_execution_id_and_sequence(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = seed_session(sessions_dal, tmp_path)
    sessions_dal.record_session_error(session_id, "spawn crashed")
    rows = sessions_dal._connection.execute(  # noqa: SLF001
        "SELECT execution_id, sequence FROM events WHERE action = 'session.spawn_failed' "
        "AND target_id = ?",
        (session_id,),
    ).fetchall()
    assert len(rows) == 1
    # session.created already claimed sequence 1, so the error is 2 -- proof
    # this site shares the SAME per-session counter as the lifecycle events,
    # not an independent one.
    assert rows[0]["execution_id"] == session_id
    assert rows[0]["sequence"] == 2


def test_record_max_park_extension_populates_execution_id_and_sequence(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = seed_session(sessions_dal, tmp_path)
    sessions_dal.record_max_park_extension(session_id, approval_id=None, detail="re-armed")
    sessions_dal.record_max_park_extension(session_id, approval_id=None, detail="re-armed again")
    rows = sessions_dal._connection.execute(  # noqa: SLF001
        "SELECT sequence FROM events WHERE action = 'reaper.max_park_extended' "
        "AND execution_id = ? ORDER BY id ASC",
        (session_id,),
    ).fetchall()
    # session.created = 1, then the two max-park re-arms.
    assert [row["sequence"] for row in rows] == [2, 3]


def test_create_session_approval_emits_approval_requested_event(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = seed_session(sessions_dal, tmp_path)
    approval_id = sessions_dal.create_session_approval(
        session_id, "h", "consequential", "risky", "{}", "high", "", None
    )
    rows = sessions_dal._connection.execute(  # noqa: SLF001 - verify the transactional emit
        "SELECT type, action, payload_json, execution_id, sequence FROM events "
        "WHERE type = 'approval.requested'"
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload == {
        "approval_id": approval_id,
        "session_id": session_id,
        "action_class": "consequential",
        "state": "pending",
    }
    # W2.6: scoped to the owning SESSION's execution_id (not approval_id), so it
    # shares the same per-session counter as session.created (sequence 1).
    assert rows[0]["execution_id"] == session_id
    assert rows[0]["sequence"] == 2
    # Idempotent re-request does not re-emit.
    sessions_dal.create_session_approval(
        session_id, "h", "consequential", "risky", "{}", "high", "", None
    )
    again = sessions_dal._connection.execute(  # noqa: SLF001
        "SELECT COUNT(*) AS n FROM events WHERE type = 'approval.requested'"
    ).fetchone()
    assert again["n"] == 1


def test_approval_counts_aggregates_by_session_and_state(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    # T-CODE-005: per-session totals grouped by state (requested = all, granted =
    # approved, denied = rejected). Only THIS session's approvals are counted.
    session_id = seed_session(sessions_dal, tmp_path, session_id="ses_counts")
    other = seed_session(sessions_dal, tmp_path, session_id="ses_other")
    ids = [
        sessions_dal.create_session_approval(
            session_id, f"h{i}", "consequential", "x", "{}", "high", "", None
        )
        for i in range(3)
    ]
    sessions_dal.create_session_approval(other, "hz", "consequential", "x", "{}", "high", "", None)
    # Decide two of this session's approvals: one approved, one rejected.
    sessions_dal._connection.execute(  # noqa: SLF001 - arrange decided states directly
        "UPDATE approvals SET state = 'approved' WHERE id = ?", (ids[0],)
    )
    sessions_dal._connection.execute(  # noqa: SLF001
        "UPDATE approvals SET state = 'rejected' WHERE id = ?", (ids[1],)
    )
    sessions_dal._connection.commit()

    counts = sessions_dal.approval_counts(session_id)
    assert counts == {
        "approvals_requested": 3,
        "approvals_granted": 1,
        "approvals_denied": 1,
    }
    # A session with no approvals returns zeros.
    empty = seed_session(sessions_dal, tmp_path, session_id="ses_none")
    assert sessions_dal.approval_counts(empty) == {
        "approvals_requested": 0,
        "approvals_granted": 0,
        "approvals_denied": 0,
    }


def test_terminalize_state_and_void_are_one_transaction(
    sessions_dal: SessionsDal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-OPS-007: a fault between the state change and the void rolls BOTH back.

    There is no window where a terminal session still holds a pending approval.
    """
    session_id = seed_session(sessions_dal, tmp_path, state="running")
    sessions_dal.create_session_approval(session_id, "h", "consequential", "x", "{}", "", "", None)

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("crash between state transition and approval void")

    monkeypatch.setattr(sessions_dal, "_event", boom)
    with pytest.raises(RuntimeError):
        sessions_dal.terminalize_session(session_id, "killed", killed_by="operator", void_note="t")
    # Rolled back: session still non-terminal AND approval still pending.
    assert sessions_dal.get_session(session_id)["state"] == "running"  # type: ignore[index]
    assert sessions_dal.get_latest_session_approval(session_id)["state"] == "pending"  # type: ignore[index]


def test_terminalize_persists_killed_by_and_voids(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    session_id = seed_session(sessions_dal, tmp_path, state="running")
    sessions_dal.create_session_approval(session_id, "h", "consequential", "x", "{}", "", "", None)
    assert sessions_dal.terminalize_session(
        session_id, "killed", killed_by="operator", void_note="terminal"
    )
    session = sessions_dal.get_session(session_id)
    assert session["killed_by"] == "operator"  # type: ignore[index]
    assert session["state"] == "killed"  # type: ignore[index]
    assert sessions_dal.get_latest_session_approval(session_id)["state"] == "expired"  # type: ignore[index]


def test_request_kill_does_not_overwrite_operator_cancel_attribution(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """F1 pin (exact overwrite scenario): request_cancel sets
    killed_by='cancel_requested'; a later reaper request_kill('idle-reaper')
    must NOT re-attribute — otherwise the D7 blocklist would respawn an
    operator-cancelled task. The reaper call stays a no-op-but-True."""
    session_id = seed_session(sessions_dal, tmp_path, state="running")
    assert sessions_dal.request_cancel(session_id)
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["killed_by"] == "cancel_requested"
    assert row["kill_requested"] == 1

    # Later reaper kill: still True (session is non-terminal) but no re-attribution.
    assert sessions_dal.request_kill(session_id, killed_by="idle-reaper")
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["killed_by"] == "cancel_requested"
    assert row["kill_requested"] == 1


def test_request_kill_first_attribution_wins_and_terminal_stays_false(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """First attribution wins between reapers too, an unattributed repeat does
    not clear it, and the CAS guard still returns False on terminal rows."""
    session_id = seed_session(sessions_dal, tmp_path, state="running")
    assert sessions_dal.request_kill(session_id, killed_by="idle-reaper")
    assert sessions_dal.request_kill(session_id, killed_by="budget")  # no-op-but-True
    assert sessions_dal.request_kill(session_id)  # unattributed repeat: True, no clear
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["killed_by"] == "idle-reaper"
    assert row["kill_requested"] == 1

    assert sessions_dal.terminalize_session(
        session_id, "killed", killed_by="idle-reaper", void_note="t"
    )
    assert not sessions_dal.request_kill(session_id, killed_by="budget")


def test_terminalize_natural_exit_preserves_cancel_requested(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """F2 pin: a natural exit (killed_by=None) after request_cancel must keep
    killed_by='cancel_requested' (COALESCE semantics) so the D7 blocklist
    classifies the session as operator-superseded, not a normal completion."""
    session_id = seed_session(sessions_dal, tmp_path, state="running")
    assert sessions_dal.request_cancel(session_id)
    assert sessions_dal.terminalize_session(
        session_id, "completed", killed_by=None, void_note="natural exit"
    )
    session = sessions_dal.get_session(session_id)
    assert session is not None
    assert session["state"] == "completed"
    assert session["killed_by"] == "cancel_requested"


def test_terminalize_without_prior_kill_leaves_killed_by_null(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    """COALESCE regression guard: an ordinary natural exit with no prior
    attribution still terminalizes with killed_by NULL."""
    session_id = seed_session(sessions_dal, tmp_path, state="running")
    assert sessions_dal.terminalize_session(
        session_id, "completed", killed_by=None, void_note="natural exit"
    )
    session = sessions_dal.get_session(session_id)
    assert session is not None
    assert session["killed_by"] is None


@pytest.mark.parametrize(
    ("approval_state", "consumed_at", "expected_result", "expected_session_state"),
    [
        ("pending", None, "parked", "awaiting_approval"),
        ("approved", None, "parked", "awaiting_approval"),
        ("approved", "2026-07-23T00:00:00Z", "terminalized", "killed"),
        ("rejected", None, "terminalized", "killed"),
        (None, None, "terminalized", "killed"),
    ],
)
def test_reconcile_park_or_kill_actionable_approval_matrix(
    sessions_dal: SessionsDal,
    tmp_path: Path,
    approval_state: str | None,
    consumed_at: str | None,
    expected_result: str,
    expected_session_state: str,
) -> None:
    session_id = seed_session(sessions_dal, tmp_path, state="running", pid=99999)
    approval_id: str | None = None
    if approval_state is not None:
        approval_id = sessions_dal.create_session_approval(
            session_id, "hash", "consequential", "risky", "{}", "high", "", None
        )
        sessions_dal._connection.execute(  # noqa: SLF001 - arrange the matrix row
            "UPDATE approvals SET state = ?, consumed_at = ? WHERE id = ?",
            (approval_state, consumed_at, approval_id),
        )
        sessions_dal._connection.commit()  # noqa: SLF001

    assert sessions_dal.reconcile_park_or_kill(session_id, "killed") == expected_result
    session = sessions_dal.get_session(session_id)
    assert session is not None
    assert session["state"] == expected_session_state
    assert session["killed_by"] == ("reconcile" if expected_result == "terminalized" else None)
    if approval_id is not None:
        approval = sessions_dal.get_approval_by_id(approval_id)
        assert approval is not None
        expected_approval_state = (
            "pending"
            if approval_state == "pending" and expected_result == "parked"
            else approval_state
        )
        assert approval["state"] == expected_approval_state


@pytest.mark.parametrize(
    "state",
    ["awaiting_approval", "killed", "failed", "completed"],
)
def test_reconcile_park_or_kill_already_settled_is_unchanged(
    sessions_dal: SessionsDal, tmp_path: Path, state: str
) -> None:
    session_id = seed_session(sessions_dal, tmp_path, state=state, pid=99999)

    assert sessions_dal.reconcile_park_or_kill(session_id, "killed") == "unchanged"
    assert sessions_dal.get_session(session_id)["state"] == state  # type: ignore[index]


def test_spawn_queue_claim_and_result(sessions_dal: SessionsDal, tmp_path: Path) -> None:
    session_id = seed_session(sessions_dal, tmp_path)
    request_id = sessions_dal.enqueue_spawn(
        session_id=session_id,
        project_dir=str(tmp_path),
        model="haiku",
        prompt="go",
        budget_usd_max=None,
        title=None,
    )
    assert sessions_dal.has_queued_spawn(session_id)
    claimed = sessions_dal.claim_spawn_requests()
    assert [row["id"] for row in claimed] == [request_id]
    assert sessions_dal.mark_spawn_result(request_id, "launched")
    # Claim is single-use: a second claim finds nothing queued.
    assert not sessions_dal.mark_spawn_result(request_id, "launched")
    assert not sessions_dal.has_queued_spawn(session_id)


def test_connection_constructor_migrates_in_memory_database() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    dal = SessionsDal(connection)
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
    ).fetchone()
    dal.close()
    assert connection.execute("SELECT 1").fetchone()[0] == 1


def test_invalid_session_id_and_unknown_fields_fail_closed(
    sessions_dal: SessionsDal, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="ses_ prefix"):
        sessions_dal.create_session(
            {"id": "wrong", "source": "bridge", "project_dir": str(tmp_path)}
        )
    with pytest.raises(ValueError, match="unknown columns"):
        sessions_dal.create_session(
            {
                "id": "ses_bad",
                "source": "bridge",
                "project_dir": str(tmp_path),
                "secret": "must not persist",
            }
        )


def test_max_park_extension_sequence_dense_across_concurrent_connections(
    tmp_path: Path,
) -> None:
    """Race-safety across CONNECTIONS, not just in-process locking: N separate
    ``SessionsDal`` instances (own connection each, as two live processes would
    have) writing ``record_max_park_extension`` for the SAME session concurrently
    must still land the exact dense set {1..N+1} (created + N re-arms) with no
    duplicate and no gap -- proof the per-session sequence read-then-insert is
    atomic across the whole database file (BEGIN IMMEDIATE), not merely across
    one process's ``self._lock``.
    """
    from tests.support.db_template import migrated_db

    db_path = migrated_db(SessionsDal, tmp_path / "sessions_concurrent.db")
    seed_dal = SessionsDal(db_path)
    session_id = seed_session(seed_dal, tmp_path)
    seed_dal.close()

    worker_count = 8

    def write_extension(worker: int) -> None:
        dal = SessionsDal(db_path)
        try:
            dal.record_max_park_extension(
                session_id, approval_id=None, detail=f"worker-{worker}"
            )
        finally:
            dal.close()

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        for future in [pool.submit(write_extension, worker) for worker in range(worker_count)]:
            future.result()

    verify_dal = SessionsDal(db_path)
    rows = verify_dal._connection.execute(  # noqa: SLF001
        "SELECT sequence FROM events WHERE execution_id = ?", (session_id,)
    ).fetchall()
    verify_dal.close()
    sequences = sorted(row["sequence"] for row in rows)
    # session.created claims sequence 1; the 8 concurrent re-arms must fill
    # 2..9 densely with no gap or duplicate.
    assert sequences == list(range(1, worker_count + 2)), (
        "sequence must be the exact dense set 1..N+1 with no gap or duplicate"
    )
