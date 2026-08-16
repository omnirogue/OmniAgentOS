from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.improve.dispatcher import (
    IMPROVE_APPROVAL_ACTION_CLASS,
    NON_TERMINAL_SAGA_STATES,
    ImproveTest,
    SequencerError,
    emit_worker_result,
    ensure_improve_dispatcher_routine,
    ingest_worker_results,
    parse_improve_mode,
    reconcile_saga,
    resolve_improve_mode,
    schedule_wave,
    sequence_waves,
    tick,
)
from omniagentos.scheduler.routines import (
    IMPROVE_DISPATCHER_CRON,
    IMPROVE_DISPATCHER_ROUTINE_NAME,
    cron_is_due,
    improve_dispatcher_routine,
    validate_routine,
)
from tests.support.db_template import make_store, migrated_db


class FakeRefs:
    def __init__(self, head: str, commits: set[str], ancestors: set[tuple[str, str]]) -> None:
        self.head = head
        self.commits = commits
        self.ancestors = ancestors

    def head_sha(self) -> str:
        return self.head

    def commit_exists(self, sha: str) -> bool:
        return sha in self.commits

    def is_ancestor(self, sha: str, descendant: str) -> bool:
        return (sha, descendant) in self.ancestors


@pytest.fixture
def db_conn(tmp_path: Path) -> sqlite3.Connection:
    db = migrated_db(SqliteStore, tmp_path / "improve.db")
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@pytest.fixture
def sqlite_store(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "improve_store.db")


def insert_approval(
    conn: sqlite3.Connection,
    app_id: str,
    action_class: str,
    state: str,
    decided_by: str | None,
    expires_at: str | None = None,
    created_at: str = "2026-07-27T00:00:00Z",
) -> None:
    conn.execute(
        "INSERT INTO approvals (id, action_class, state, decided_by, expires_at, created_at, proposed_action) "
        "VALUES (?, ?, ?, ?, ?, ?, '')",
        (app_id, action_class, state, decided_by, expires_at, created_at),
    )


def test_flag_defaults_off_and_rejects_truthiness(
    db_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. parse_improve_mode checks
    assert parse_improve_mode(None) == "off"
    for v in ["1", "true", "yes", "enabled", "", "ON!", "Off"]:
        assert parse_improve_mode(v) == "off"
    assert parse_improve_mode(" On ") == "on"

    # 2. tick() with var unset performs zero writes
    monkeypatch.delenv("OMNIAGENTOS_IMPROVE_MODE", raising=False)

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    worker_file = inbox / "run_123.json"
    worker_file.write_text(
        '{"run_id": "run_123", "test_id": "test_1", "category": "cat", "tier": "T1", "status": "pass"}',
        encoding="utf-8",
    )

    before_saga = db_conn.execute("SELECT COUNT(*) FROM improve_saga").fetchone()[0]
    before_runs = db_conn.execute("SELECT COUNT(*) FROM configtest_runs").fetchone()[0]

    fake_refs = FakeRefs("head", {"head"}, {("head", "head")})
    res = tick(db_conn, refs=fake_refs, inbox=inbox, tests=[])

    assert res["mode"] == "off"
    assert res["requested"] == "off"

    after_saga = db_conn.execute("SELECT COUNT(*) FROM improve_saga").fetchone()[0]
    after_runs = db_conn.execute("SELECT COUNT(*) FROM configtest_runs").fetchone()[0]

    assert before_saga == after_saga == 0
    assert before_runs == after_runs == 0
    assert worker_file.exists()


def test_on_without_approval_row_refuses_to_enable(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    env = {"OMNIAGENTOS_IMPROVE_MODE": "on"}
    decision = resolve_improve_mode(db_conn, env=env)
    assert decision.requested == "on"
    assert decision.effective == "shadow"
    assert decision.approved_by is None
    assert "no approved_by row" in decision.reason

    # tick() with schedulable test
    t = ImproveTest("test_1", frozenset(["foo"]))
    fake_refs = FakeRefs("head", {"head"}, {("head", "head")})
    inbox = tmp_path / "inbox"

    res = tick(db_conn, refs=fake_refs, inbox=inbox, tests=[t], env=env)
    assert res["mode"] == "shadow"
    assert res["dispatched"] == ["test_1"]

    saga_count = db_conn.execute("SELECT COUNT(*) FROM improve_saga").fetchone()[0]
    assert saga_count == 0


def test_on_with_approval_row_reserves(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    env = {"OMNIAGENTOS_IMPROVE_MODE": "on"}

    # 1. Approval with state='pending' (does not count)
    insert_approval(
        db_conn,
        "apr_1",
        IMPROVE_APPROVAL_ACTION_CLASS,
        "pending",
        "owner",
        created_at="2026-07-27T00:00:00Z",
    )
    decision = resolve_improve_mode(db_conn, env=env, now="2026-07-27T01:00:00Z")
    assert decision.effective == "shadow"

    # 2. Approval with expires_at in the past (does not count)
    insert_approval(
        db_conn,
        "apr_2",
        IMPROVE_APPROVAL_ACTION_CLASS,
        "approved",
        "owner",
        expires_at="2026-07-27T00:30:00Z",
        created_at="2026-07-27T00:00:00Z",
    )
    decision = resolve_improve_mode(db_conn, env=env, now="2026-07-27T01:00:00Z")
    assert decision.effective == "shadow"

    # 3. Valid approval (state='approved', active, non-empty decided_by)
    insert_approval(
        db_conn,
        "apr_3",
        IMPROVE_APPROVAL_ACTION_CLASS,
        "approved",
        "owner",
        expires_at="2026-07-27T02:00:00Z",
        created_at="2026-07-27T00:00:00Z",
    )
    decision = resolve_improve_mode(db_conn, env=env, now="2026-07-27T01:00:00Z")
    assert decision.effective == "on"
    assert decision.approved_by == "owner"

    # tick() INSERTS a CALL_RESERVED row
    t = ImproveTest("test_1", frozenset(["foo"]))
    fake_refs = FakeRefs("head", {"head"}, {("head", "head")})
    inbox = tmp_path / "inbox"

    res = tick(
        db_conn, refs=fake_refs, inbox=inbox, tests=[t], env=env, now="2026-07-27T01:00:00Z"
    )
    assert res["mode"] == "on"
    assert res["dispatched"] == ["test_1"]

    rows = db_conn.execute(
        "SELECT attempt_id, state, idempotency_key, updated_at FROM improve_saga"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["attempt_id"] == "att_test_1_2026-07-27T01:00:00Z"
    assert rows[0]["state"] == "CALL_RESERVED"
    assert rows[0]["idempotency_key"] == "improve:test_1:2026-07-27T01:00:00Z"
    assert rows[0]["updated_at"] == "2026-07-27T01:00:00Z"


def test_intersecting_closures_never_scheduled_concurrently() -> None:
    a = ImproveTest("A", frozenset({"module_x", "module_z"}))
    b = ImproveTest("B", frozenset({"module_x", "module_w"}))
    c = ImproveTest("C", frozenset({"module_y"}))

    tests = [a, b, c]

    # schedule_wave iterate sorted by test_id. A and C run, B is skipped.
    wave0 = schedule_wave(tests)
    wave0_ids = {t.test_id for t in wave0}
    assert "A" in wave0_ids
    assert "C" in wave0_ids
    assert "B" not in wave0_ids

    # waves sequencing
    waves = sequence_waves(tests)
    assert waves == [["A", "C"], ["B"]]

    # schedule_wave(..., running=[A]) excludes B
    wave_with_running = schedule_wave(tests, running=[a])
    wave_with_running_ids = {t.test_id for t in wave_with_running}
    assert "B" not in wave_with_running_ids
    assert "C" in wave_with_running_ids


def test_dependent_test_waits_for_prerequisite() -> None:
    a = ImproveTest("a", frozenset({"mod_a"}))
    b = ImproveTest("b", frozenset({"mod_b"}), depends_on=("a",))

    tests = [a, b]

    # wave 0 has a and not b
    wave0 = schedule_wave(tests)
    wave0_ids = {t.test_id for t in wave0}
    assert "a" in wave0_ids
    assert "b" not in wave0_ids

    # with completed={'a'}, b is admitted
    wave1 = schedule_wave(tests, completed={"a"})
    wave1_ids = {t.test_id for t in wave1}
    assert "b" in wave1_ids
    assert "a" not in wave1_ids

    # Unknown prerequisite raises SequencerError
    c = ImproveTest("c", frozenset({"mod_c"}), depends_on=("unknown",))
    with pytest.raises(SequencerError) as exc:
        schedule_wave([c])
    assert "unknown prerequisite" in str(exc.value)

    # A dependency cycle raises SequencerError
    d = ImproveTest("d", frozenset({"mod_d"}), depends_on=("e",))
    e = ImproveTest("e", frozenset({"mod_e"}), depends_on=("d",))
    with pytest.raises(SequencerError) as exc:
        sequence_waves([d, e])
    assert "dependency cycle" in str(exc.value)


@pytest.mark.parametrize("state", NON_TERMINAL_SAGA_STATES)
def test_saga_reconciles_from_every_non_terminal_state(
    db_conn: sqlite3.Connection, state: str
) -> None:
    db_conn.execute("DELETE FROM improve_saga")

    db_conn.execute(
        "INSERT INTO improve_saga (attempt_id, state, approved_sha, merged_sha, idempotency_key, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("att_1", state, "sha_approved", "sha_merged", f"idemp:{state}:yes", "2026-07-27T00:00:00Z"),
    )

    fake_refs_on_head = FakeRefs(
        head="sha_head",
        commits={"sha_head", "sha_approved", "sha_merged"},
        ancestors={("sha_approved", "sha_head"), ("sha_merged", "sha_head")},
    )

    reconciled = reconcile_saga(db_conn, fake_refs_on_head, now="2026-07-27T01:00:00Z")
    assert len(reconciled) == 1
    rec = reconciled[0]
    assert rec.attempt_id == "att_1"
    assert rec.previous_state == state

    row = db_conn.execute(
        "SELECT state, approved_sha, merged_sha, updated_at FROM improve_saga WHERE attempt_id='att_1'"
    ).fetchone()
    assert row is not None

    if state == "CALL_RESERVED":
        assert rec.new_state == "REVERTED"
        assert rec.reason == "reserved but never sent"
        assert row["state"] == "REVERTED"
    elif state == "CALL_SENT":
        assert rec.new_state == "REVERTED"
        assert rec.reason == "sent but never approved"
        assert row["state"] == "REVERTED"
    elif state == "APPROVED_SHA":
        assert rec.new_state == "VERIFYING"
        assert rec.reason == "approved sha already on HEAD; verification owed"
        assert row["state"] == "VERIFYING"
        assert row["merged_sha"] == "sha_approved"
    elif state == "MERGE_INTENT":
        assert rec.new_state == "VERIFYING"
        assert rec.reason == "merge intent landed on HEAD; verification owed"
        assert row["state"] == "VERIFYING"
        assert row["merged_sha"] == "sha_approved"
    elif state == "MERGED_SHA":
        assert rec.new_state == "VERIFYING"
        assert rec.reason == "merged sha on HEAD; verification owed"
        assert row["state"] == "VERIFYING"
    elif state == "VERIFYING":
        assert rec.new_state == "VERIFYING"
        assert rec.reason == "verification still owed"
        assert row["state"] == "VERIFYING"

    assert row["updated_at"] == "2026-07-27T01:00:00Z"


@pytest.mark.parametrize("state", NON_TERMINAL_SAGA_STATES)
def test_saga_reconciles_from_every_non_terminal_state_not_on_head(
    db_conn: sqlite3.Connection, state: str
) -> None:
    db_conn.execute("DELETE FROM improve_saga")

    db_conn.execute(
        "INSERT INTO improve_saga (attempt_id, state, approved_sha, merged_sha, idempotency_key, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("att_1", state, "sha_approved", "sha_merged", f"idemp:{state}:no", "2026-07-27T00:00:00Z"),
    )

    fake_refs_not_on_head = FakeRefs(
        head="sha_head", commits={"sha_head"}, ancestors=set()
    )

    reconciled = reconcile_saga(db_conn, fake_refs_not_on_head, now="2026-07-27T01:00:00Z")
    assert len(reconciled) == 1
    rec = reconciled[0]
    assert rec.attempt_id == "att_1"
    assert rec.previous_state == state

    row = db_conn.execute(
        "SELECT state, updated_at FROM improve_saga WHERE attempt_id='att_1'"
    ).fetchone()
    assert row is not None

    if state in ("CALL_RESERVED", "CALL_SENT", "APPROVED_SHA", "MERGE_INTENT", "MERGED_SHA"):
        assert rec.new_state == "REVERTED"
        assert row["state"] == "REVERTED"
    elif state == "VERIFYING":
        assert rec.new_state == "VERIFYING"
        assert row["state"] == "VERIFYING"

    assert row["updated_at"] == "2026-07-27T01:00:00Z"


def test_unverified_head_blocks_the_queue(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    env = {"OMNIAGENTOS_IMPROVE_MODE": "on"}
    insert_approval(
        db_conn,
        "apr_1",
        IMPROVE_APPROVAL_ACTION_CLASS,
        "approved",
        "owner",
        expires_at="2026-07-27T02:00:00Z",
        created_at="2026-07-27T00:00:00Z",
    )

    db_conn.execute(
        "INSERT INTO improve_saga (attempt_id, state, approved_sha, merged_sha, idempotency_key, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("att_blocker", "MERGED_SHA", None, "sha_merged", "idemp:blocker", "2026-07-27T00:00:00Z"),
    )

    t = ImproveTest("test_schedulable", frozenset({"foo"}))
    fake_refs = FakeRefs(
        head="sha_head", commits={"sha_head", "sha_merged"}, ancestors={("sha_merged", "sha_head")}
    )
    inbox = tmp_path / "inbox"

    res1 = tick(
        db_conn, refs=fake_refs, inbox=inbox, tests=[t], env=env, now="2026-07-27T01:00:00Z"
    )
    assert res1["blocked_by"] == "att_blocker"
    assert res1["dispatched"] == []

    reserved_count = db_conn.execute(
        "SELECT COUNT(*) FROM improve_saga WHERE state='CALL_RESERVED'"
    ).fetchone()[0]
    assert reserved_count == 0

    db_conn.execute("UPDATE improve_saga SET state='VERIFIED' WHERE attempt_id='att_blocker'")

    res2 = tick(
        db_conn, refs=fake_refs, inbox=inbox, tests=[t], env=env, now="2026-07-27T01:05:00Z"
    )
    assert res2["blocked_by"] is None
    assert res2["dispatched"] == ["test_schedulable"]

    rows = db_conn.execute(
        "SELECT attempt_id, state FROM improve_saga WHERE attempt_id='att_test_schedulable_2026-07-27T01:05:00Z'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["state"] == "CALL_RESERVED"


def test_single_writer_workers_emit_files_only(
    db_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = tmp_path / "inbox"
    payload = {
        "run_id": "run_1",
        "test_id": "test_1",
        "category": "cat_1",
        "tier": "T1",
        "status": "pass",
        "formation_json": "{}",
        "models_json": "[]",
        "bracket_budget_json": "{}",
    }

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("No DB allowed for workers!")

    monkeypatch.setattr(sqlite3, "connect", explode)

    emitted_path = emit_worker_result(inbox, payload)
    assert emitted_path.exists()
    assert emitted_path.name == "run_1.json"

    monkeypatch.undo()

    ingested = ingest_worker_results(db_conn, inbox, now="2026-07-27T01:00:00Z")
    assert ingested == ["run_1"]
    assert not emitted_path.exists()

    runs = db_conn.execute("SELECT * FROM configtest_runs WHERE run_id='run_1'").fetchall()
    assert len(runs) == 1
    assert runs[0]["test_id"] == "test_1"
    assert runs[0]["status"] == "pass"

    emit_worker_result(inbox, payload)
    ingested_again = ingest_worker_results(db_conn, inbox, now="2026-07-27T01:05:00Z")
    assert ingested_again == ["run_1"]

    runs_again = db_conn.execute("SELECT * FROM configtest_runs WHERE run_id='run_1'").fetchall()
    assert len(runs_again) == 1

    malformed_path = inbox / "run_malformed.json"
    malformed_path.write_text("{bad json...", encoding="utf-8")

    ingested_malformed = ingest_worker_results(db_conn, inbox, now="2026-07-27T01:10:00Z")
    assert ingested_malformed == []
    assert not malformed_path.exists()
    assert (inbox / "run_malformed.rejected").exists()


def test_improve_dispatcher_routine_is_valid_and_registers_once(sqlite_store: SqliteStore) -> None:
    payload = improve_dispatcher_routine()
    validate_routine(payload)

    cron_expr = IMPROVE_DISPATCHER_CRON
    due_05 = cron_is_due(cron_expr, datetime(2026, 7, 27, 12, 5), last_fired=None)
    assert due_05 is True

    due_07 = cron_is_due(
        cron_expr, datetime(2026, 7, 27, 12, 7), last_fired="2026-07-27T12:05:00Z"
    )
    assert due_07 is False

    res1 = ensure_improve_dispatcher_routine(sqlite_store)
    res2 = ensure_improve_dispatcher_routine(sqlite_store)
    assert res1["name"] == IMPROVE_DISPATCHER_ROUTINE_NAME
    assert res2["name"] == IMPROVE_DISPATCHER_ROUTINE_NAME

    rows = sqlite_store._connection.execute(
        "SELECT COUNT(*) FROM routines WHERE name = ?", (IMPROVE_DISPATCHER_ROUTINE_NAME,)
    ).fetchone()
    assert rows[0] == 1


def test_reverted_attempt_is_re_reserved_after_restart(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    env = {"OMNIAGENTOS_IMPROVE_MODE": "on"}
    insert_approval(
        db_conn,
        "apr_1",
        IMPROVE_APPROVAL_ACTION_CLASS,
        "approved",
        "owner",
        expires_at="2026-07-27T02:00:00Z",
        created_at="2026-07-27T00:00:00Z",
    )
    test = ImproveTest("t1", frozenset({"m"}))
    fake_refs = FakeRefs(head="sha_head", commits={"sha_head"}, ancestors=set())
    inbox = tmp_path / "inbox"

    # tick at now="2026-07-27T01:00:00Z" -> assert exactly one row, CALL_RESERVED
    res1 = tick(db_conn, refs=fake_refs, inbox=inbox, tests=[test], env=env, now="2026-07-27T01:00:00Z")
    assert res1["dispatched"] == ["t1"]

    rows1 = db_conn.execute("SELECT attempt_id, state FROM improve_saga").fetchall()
    assert len(rows1) == 1
    assert rows1[0]["state"] == "CALL_RESERVED"
    assert rows1[0]["attempt_id"] == "att_t1_2026-07-27T01:00:00Z"

    # tick again at now="2026-07-27T01:05:00Z" (simulating the restart after the crash) -> assert:
    # the first attempt row is now REVERTED, AND a SECOND, DISTINCT attempt_id row exists in state
    # CALL_RESERVED for the same test, AND result["dispatched"] == ["t1"].
    # Assert SELECT COUNT(*) FROM improve_saga == 2 and the two attempt_ids differ.
    res2 = tick(db_conn, refs=fake_refs, inbox=inbox, tests=[test], env=env, now="2026-07-27T01:05:00Z")
    assert res2["dispatched"] == ["t1"]

    rows2 = db_conn.execute("SELECT attempt_id, state FROM improve_saga ORDER BY updated_at ASC").fetchall()
    assert len(rows2) == 2
    assert rows2[0]["state"] == "REVERTED"
    assert rows2[0]["attempt_id"] == "att_t1_2026-07-27T01:00:00Z"
    assert rows2[1]["state"] == "CALL_RESERVED"
    assert rows2[1]["attempt_id"] == "att_t1_2026-07-27T01:05:00Z"
    assert rows2[0]["attempt_id"] != rows2[1]["attempt_id"]

    count = db_conn.execute("SELECT COUNT(*) FROM improve_saga").fetchone()[0]
    assert count == 2


def test_dispatched_reports_only_actual_reservations(db_conn: sqlite3.Connection, tmp_path: Path) -> None:
    env = {"OMNIAGENTOS_IMPROVE_MODE": "on"}
    insert_approval(
        db_conn,
        "apr_1",
        IMPROVE_APPROVAL_ACTION_CLASS,
        "approved",
        "owner",
        expires_at="2026-07-27T02:00:00Z",
        created_at="2026-07-27T00:00:00Z",
    )
    test = ImproveTest("t1", frozenset({"m"}))
    fake_refs = FakeRefs(head="sha_head", commits={"sha_head"}, ancestors=set())
    inbox = tmp_path / "inbox"

    # Pre-insert an improve_saga row that collides on the UNIQUE idempotency_key the tick will use
    # (improve:t1:<now>) with a DIFFERENT attempt_id, in a TERMINAL state (VERIFIED, so it does not block the queue)
    now_val = "2026-07-27T01:00:00Z"
    db_conn.execute(
        "INSERT INTO improve_saga (attempt_id, state, idempotency_key, updated_at) "
        "VALUES (?, 'VERIFIED', ?, ?)",
        ("att_different", f"improve:t1:{now_val}", now_val),
    )

    # Run tick(...) with that same now. Assert result["dispatched"] == []
    res = tick(db_conn, refs=fake_refs, inbox=inbox, tests=[test], env=env, now=now_val)
    assert res["dispatched"] == []

    # Assert no new CALL_RESERVED row exists
    rows = db_conn.execute("SELECT attempt_id, state FROM improve_saga WHERE state = 'CALL_RESERVED'").fetchall()
    assert len(rows) == 0

    # Assert only the original row exists
    all_rows = db_conn.execute("SELECT attempt_id, state FROM improve_saga").fetchall()
    assert len(all_rows) == 1
    assert all_rows[0]["attempt_id"] == "att_different"

