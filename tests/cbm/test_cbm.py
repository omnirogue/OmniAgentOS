"""Cognitive Budget Manager — allocate, escalate, contract, learn."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from omniagentos.cbm.service import RUNGS, CbmCloseRetryableError, CognitiveBudgetService
from omniagentos.db.store import SqliteStore
from tests.support.db_template import migrated_db


class LearningSink:
    """Reader for the durable close-learning file (M-47).

    Delivery assertions read bytes off disk. Reading the learning API's
    in-memory list instead would let a delivery that never became durable pass
    every test, which is exactly the defect this suite has to catch.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def __iter__(self) -> Iterator[dict]:
        return iter(self.records())

    def clear(self) -> None:
        self.path.write_text("", encoding="utf-8")


@pytest.fixture()
def learning_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LearningSink]:
    """Pin the durable learning sink to a per-test file."""
    path = tmp_path / "learning" / "sink.jsonl"
    monkeypatch.setenv("OMNIAGENTOS_CBM_LEARNING_LOG", str(path))
    yield LearningSink(path)


@pytest.fixture()
def svc(tmp_path: Path, learning_log: LearningSink) -> CognitiveBudgetService:
    # Depends on learning_log so the sink override is in place before the
    # service resolves it in __init__.
    return CognitiveBudgetService(database=migrated_db(SqliteStore, tmp_path / "cbm.db"))


def _outbox(svc: CognitiveBudgetService, allocation_id: str) -> dict:
    row = svc._connection.execute(
        "SELECT * FROM cbm_close_outbox WHERE allocation_id = ?", (allocation_id,)
    ).fetchone()
    assert row is not None, "close outbox row must be durable"
    return dict(row)


def _counts(svc: CognitiveBudgetService, allocation_id: str) -> tuple[int, int]:
    outcomes = svc._connection.execute(
        "SELECT COUNT(*) AS n FROM cbm_outcomes WHERE allocation_id = ?", (allocation_id,)
    ).fetchone()["n"]
    samples = svc._connection.execute(
        "SELECT COALESCE(SUM(samples), 0) AS n FROM cbm_role_stats"
    ).fetchone()["n"]
    return int(outcomes), int(samples)


def _kinds(log: LearningSink) -> list[str]:
    """Durably emitted learning steps, in order.

    ``learning.api._record`` merges the payload over the record envelope, so a
    CBM decision event surfaces as its own ``kind`` (``cbm_allocation_close``)
    while ``attach_outcome`` keeps ``kind == "outcome"``.
    """
    steps = []
    for row in log.records():
        kind = str(row.get("kind"))
        if kind == "cbm_allocation_close":
            steps.append("decision")
        elif kind == "outcome":
            steps.append("outcome")
        else:  # pragma: no cover - unexpected emission must not pass silently
            steps.append(f"unexpected:{kind}")
    return steps


def test_health_live_no_cost_objective(svc: CognitiveBudgetService) -> None:
    h = svc.health()
    assert h["ok"] is True
    assert h["live"] is True
    assert h["cost_objective"] is False
    assert h["objective"] == "quality+ETAR"
    assert h["default_start_rung"] == 1
    assert h["rungs"] == 7


def test_fast_first_default(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(task_id="t1", stage="execution")
    assert a["rung"] == 1
    assert a["model_role"] == "fast_implementer"
    assert a["reasoning_effort"] == "low"
    assert a["status"] == "active"
    assert "codex" in a["provider_hints"] or a["provider_hints"]


def test_irreversible_starts_higher(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(
        task_id="t2",
        stage="execution",
        risk_class="irreversible",
        novelty="high",
    )
    assert a["rung"] >= 4


def test_verification_stage_mechanical(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(stage="verification")
    assert a["rung"] == 0
    assert a["model_role"] == "none"


def test_escalate_on_evidence(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(task_id="esc1")
    e = svc.escalate(
        a["id"],
        trigger_code="gate_failure",
        evidence=["tests failed: 2 errors"],
        next_gate="re_run_tests",
    )
    assert e["from_rung"] == 1
    assert e["to_rung"] == 2
    assert e["changed"]["reasoning_effort"] == "high"
    got = svc.get_allocation(a["id"])
    assert got is not None
    assert got["status"] == "escalated"
    assert got["rung"] == 2
    esc = svc.list_escalations(a["id"])
    assert len(esc) == 1
    assert esc[0]["trigger_code"] == "gate_failure"


def test_repeated_failure_forces_diverse(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(task_id="esc2")
    e = svc.escalate(a["id"], trigger_code="repeated_failure", evidence=["same hash"])
    assert e["to_rung"] >= 3


def test_contract_and_close_updates_leaderboard(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(task_id="close1")
    svc.contract(a["id"], reason="gate_passed")
    closed = svc.close_allocation(
        a["id"],
        first_pass_accepted=True,
        wall_seconds=42.0,
        repair_count=0,
        verifier_score=0.99,
    )
    assert closed["closed"] is True
    board = svc.leaderboard()
    assert any(r["role"] == "fast_implementer" and r["samples"] >= 1 for r in board)


def test_rungs_cover_0_to_6() -> None:
    assert len(RUNGS) == 7
    assert RUNGS[0]["name"] == "mechanical"
    assert RUNGS[6]["name"] == "break_glass"


# --- M-40: recommend_rung must be side-effect free ----------------------------


def test_recommend_rung_nested_rollback(svc: CognitiveBudgetService) -> None:
    """M-40: recommend_rung uses SAVEPOINT/ROLLBACK, leaves no probe rows."""
    board_before = svc.leaderboard()
    rec = svc.recommend_rung(required_quality=0.99, risk_class="irreversible")
    board_after = svc.leaderboard()
    # The stats should not have been updated because of nested rollback (M-40)
    assert board_before == board_after
    assert rec["recommended_rung"] >= 3
    # Probe allocation must not remain after SAVEPOINT rollback
    n_alloc = svc._connection.execute("SELECT COUNT(*) AS n FROM cbm_allocations").fetchone()
    n_out = svc._connection.execute("SELECT COUNT(*) AS n FROM cbm_outcomes").fetchone()
    assert int(n_alloc["n"]) == 0
    assert int(n_out["n"]) == 0
    # Repeated recommends must stay side-effect free (savepoint released)
    for _ in range(3):
        svc.recommend_rung(required_quality=0.99, risk_class="irreversible")
    n_alloc2 = svc._connection.execute("SELECT COUNT(*) AS n FROM cbm_allocations").fetchone()
    assert int(n_alloc2["n"]) == 0


# --- M-47: idempotent close + retryable failure -------------------------------


def test_close_allocation_idempotent(svc: CognitiveBudgetService) -> None:
    """M-47: Second close returns same outcome_id, doesn't recount stats."""
    a = svc.allocate(task_id="close-idem")
    c1 = svc.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=10.0, repair_count=0)
    samples_after_first = next(
        r["samples"] for r in svc.leaderboard() if r["role"] == "fast_implementer"
    )
    # The second call shouldn't fail and shouldn't recount (M-47)
    c2 = svc.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=20.0, repair_count=1)
    assert c1["closed"] is True
    assert c2["closed"] is True
    assert c1["outcome_id"] == c2["outcome_id"]
    assert c2.get("idempotent") is True
    samples_after_second = next(
        r["samples"] for r in svc.leaderboard() if r["role"] == "fast_implementer"
    )
    assert samples_after_first == samples_after_second == 1
    n_out = svc._connection.execute(
        "SELECT COUNT(*) AS n FROM cbm_outcomes WHERE allocation_id = ?", (a["id"],)
    ).fetchone()
    assert int(n_out["n"]) == 1


def test_close_allocation_failure_is_explicit_retryable(svc: CognitiveBudgetService) -> None:
    """M-47: Close store failure must not false-succeed; retry after repair works."""
    a = svc.allocate(task_id="close-retry")
    # Break the outcomes table so the close transaction fails before commit.
    svc._connection.execute("ALTER TABLE cbm_outcomes RENAME TO cbm_outcomes_bak")
    with pytest.raises(CbmCloseRetryableError) as ei:
        svc.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=5.0, repair_count=0)
    assert getattr(ei.value, "retryable", False) is True
    row = svc._connection.execute(
        "SELECT status FROM cbm_allocations WHERE id = ?", (a["id"],)
    ).fetchone()
    assert row["status"] == "active"
    assert svc.leaderboard() == []
    # Restore store and retry — must succeed once, never have false-succeeded above.
    svc._connection.execute("ALTER TABLE cbm_outcomes_bak RENAME TO cbm_outcomes")
    closed = svc.close_allocation(
        a["id"], first_pass_accepted=True, wall_seconds=5.0, repair_count=0
    )
    assert closed["closed"] is True
    assert closed.get("idempotent") is not True
    got = svc.get_allocation(a["id"])
    assert got is not None
    assert got["status"] == "closed"


# --- M-47: learning emission must be durable, per-step, and never false-success


def test_store_failure_reports_nothing_durable(svc: CognitiveBudgetService) -> None:
    """M-47: pre-commit failure carries durable_closed=False and both steps pending."""
    a = svc.allocate(task_id="close-nodurable")
    svc._connection.execute("ALTER TABLE cbm_outcomes RENAME TO cbm_outcomes_bak")
    try:
        with pytest.raises(CbmCloseRetryableError) as ei:
            svc.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=1.0)
    finally:
        svc._connection.execute("ALTER TABLE cbm_outcomes_bak RENAME TO cbm_outcomes")
    assert ei.value.retryable is True
    assert ei.value.durable_closed is False
    assert ei.value.learning_pending == ["log_decision", "attach_outcome"]
    assert ei.value.outcome_id is None
    # Nothing at all was committed — including the outbox row.
    n = svc._connection.execute(
        "SELECT COUNT(*) AS n FROM cbm_close_outbox WHERE allocation_id = ?", (a["id"],)
    ).fetchone()["n"]
    assert int(n) == 0
    assert _counts(svc, a["id"]) == (0, 0)


def test_first_learning_call_failure_is_explicit(
    svc: CognitiveBudgetService, learning_log: LearningSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-47: log_decision failure must not return closed=True; both steps stay pending."""
    from omniagentos.learning import api as learning_api

    a = svc.allocate(task_id="close-learn1")
    real_log_decision = learning_api.log_decision

    def boom(*_args: object, **_kwargs: object) -> dict:
        raise RuntimeError("learning sink down")

    monkeypatch.setattr(learning_api, "log_decision", boom)
    with pytest.raises(CbmCloseRetryableError) as ei:
        svc.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=3.0)

    # Truthful error fields: business close is durable, no learning delivered.
    assert ei.value.retryable is True
    assert ei.value.durable_closed is True
    assert ei.value.learning_pending == ["log_decision", "attach_outcome"]
    assert ei.value.allocation_id == a["id"]
    row = _outbox(svc, a["id"])
    assert ei.value.outcome_id == row["outcome_id"]
    assert row["log_decision_at"] is None
    assert row["attach_outcome_at"] is None
    assert int(row["attempts"]) == 1
    assert "log_decision" in str(row["last_error"])
    assert _kinds(learning_log) == []
    # Business close really is durable, exactly once.
    got = svc.get_allocation(a["id"])
    assert got is not None and got["status"] == "closed"
    assert _counts(svc, a["id"]) == (1, 1)

    # Retry with the sink repaired delivers both steps and reports success.
    monkeypatch.setattr(learning_api, "log_decision", real_log_decision)
    result = svc.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=3.0)
    assert result["closed"] is True
    assert result["idempotent"] is True
    assert result["outcome_id"] == row["outcome_id"]
    assert _kinds(learning_log) == ["decision", "outcome"]
    assert _counts(svc, a["id"]) == (1, 1)


def test_second_learning_call_failure_does_not_repeat_the_first(
    svc: CognitiveBudgetService, learning_log: LearningSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-47: attach_outcome failure keeps log_decision durably delivered."""
    from omniagentos.learning import api as learning_api

    a = svc.allocate(task_id="close-learn2")
    real_attach_outcome = learning_api.attach_outcome

    def boom(*_args: object, **_kwargs: object) -> dict:
        raise RuntimeError("outcome sink down")

    monkeypatch.setattr(learning_api, "attach_outcome", boom)
    with pytest.raises(CbmCloseRetryableError) as ei:
        svc.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=4.0)

    assert ei.value.durable_closed is True
    assert ei.value.learning_pending == ["attach_outcome"]
    row = _outbox(svc, a["id"])
    assert row["log_decision_at"] is not None
    assert row["attach_outcome_at"] is None
    assert _kinds(learning_log) == ["decision"]

    # Retry must emit only the pending step — no duplicate decision.
    monkeypatch.setattr(learning_api, "attach_outcome", real_attach_outcome)
    result = svc.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=4.0)
    assert result["closed"] is True
    assert result["idempotent"] is True
    assert result["outcome_id"] == row["outcome_id"]
    assert _kinds(learning_log) == ["decision", "outcome"]
    assert _counts(svc, a["id"]) == (1, 1)


def test_retry_after_service_reopen_uses_durable_state(
    tmp_path: Path, learning_log: LearningSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-47: pending delivery survives process/service restart and never re-closes."""
    from omniagentos.learning import api as learning_api

    db = migrated_db(SqliteStore, tmp_path / "cbm-reopen.db")
    first = CognitiveBudgetService(database=db)
    a = first.allocate(task_id="close-reopen")
    real_attach_outcome = learning_api.attach_outcome

    def boom(*_args: object, **_kwargs: object) -> dict:
        raise RuntimeError("outcome sink down")

    monkeypatch.setattr(learning_api, "attach_outcome", boom)
    with pytest.raises(CbmCloseRetryableError) as ei:
        first.close_allocation(a["id"], first_pass_accepted=False, wall_seconds=7.5, repair_count=2)
    durable_outcome_id = ei.value.outcome_id
    assert ei.value.durable_closed is True
    first.close()

    # Fresh service over the same database: state comes only from disk.
    monkeypatch.setattr(learning_api, "attach_outcome", real_attach_outcome)
    second = CognitiveBudgetService(database=db)
    try:
        result = second.close_allocation(
            a["id"], first_pass_accepted=False, wall_seconds=7.5, repair_count=2
        )
        assert result["closed"] is True
        assert result["idempotent"] is True
        assert result["outcome_id"] == durable_outcome_id
        # Only the pending step ran after the restart.
        assert _kinds(learning_log) == ["decision", "outcome"]
        assert _counts(second, a["id"]) == (1, 1)
        # The reconstructed payload carries the durable outcome id, not a new one.
        outcome_event = next(r for r in learning_log if r.get("kind") == "outcome")
        assert outcome_event["outcome"]["outcome_id"] == durable_outcome_id
        assert outcome_event["decision_id"] == a["id"]
    finally:
        second.close()


def test_repeated_successful_retry_is_stable(
    svc: CognitiveBudgetService, learning_log: LearningSink
) -> None:
    """M-47: repeated closes after success never re-emit or re-mutate."""
    a = svc.allocate(task_id="close-repeat")
    first = svc.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=2.0)
    assert first["closed"] is True
    assert first.get("idempotent") is not True
    assert _kinds(learning_log) == ["decision", "outcome"]

    for _ in range(3):
        again = svc.close_allocation(a["id"], first_pass_accepted=False, wall_seconds=99.0)
        assert again["closed"] is True
        assert again["idempotent"] is True
        assert again["outcome_id"] == first["outcome_id"]

    # No duplicate outcome rows, no extra role-stat samples, no extra emissions.
    assert _counts(svc, a["id"]) == (1, 1)
    assert _kinds(learning_log) == ["decision", "outcome"]
    row = _outbox(svc, a["id"])
    assert row["log_decision_at"] is not None
    assert row["attach_outcome_at"] is not None
    assert int(row["attempts"]) == 0


def test_pre_outbox_close_reconstructs_without_new_outcome(
    svc: CognitiveBudgetService, learning_log: LearningSink
) -> None:
    """M-47: a close recorded before the outbox existed reuses the durable outcome id."""
    a = svc.allocate(task_id="close-legacy")
    first = svc.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=6.0)
    # Simulate an allocation closed by a build that had no delivery record.
    svc._connection.execute("DELETE FROM cbm_close_outbox WHERE allocation_id = ?", (a["id"],))
    learning_log.clear()

    again = svc.close_allocation(a["id"], first_pass_accepted=False, wall_seconds=123.0)
    assert again["closed"] is True
    assert again["idempotent"] is True
    # Reconstructed, not fabricated.
    assert again["outcome_id"] == first["outcome_id"]
    assert _counts(svc, a["id"]) == (1, 1)
    # Delivery state was unknown, so re-emission is at-least-once by design, and
    # the replayed payload still carries the original durable outcome id.
    assert _kinds(learning_log) == ["decision", "outcome"]
    outcome_event = next(r for r in learning_log if r.get("kind") == "outcome")
    assert outcome_event["outcome"]["outcome_id"] == first["outcome_id"]
    # The reconstructed payload reflects the durable outcome, not the retry args.
    assert outcome_event["outcome"]["first_pass_accepted"] is True
    assert outcome_event["outcome"]["wall_seconds"] == 6.0


def test_lost_close_race_cannot_duplicate_outcome_or_stats(
    tmp_path: Path, learning_log: LearningSink
) -> None:
    """M-47: a racing second closer never writes a second outcome or role stat."""
    db = migrated_db(SqliteStore, tmp_path / "cbm-race.db")
    left = CognitiveBudgetService(database=db)
    right = CognitiveBudgetService(database=db)
    try:
        alloc = left.allocate(task_id="close-race")
        # `right` reads the allocation while it is still active, then loses the race.
        stale = dict(
            right._connection.execute(
                "SELECT * FROM cbm_allocations WHERE id = ?", (alloc["id"],)
            ).fetchone()
        )
        assert stale["status"] == "active"
        first = left.close_allocation(alloc["id"], first_pass_accepted=True, wall_seconds=1.0)

        # Replaying the stale view straight into the durable close must be
        # rejected by the outbox primary key, with nothing committed.
        with pytest.raises(CbmCloseRetryableError) as ei:
            right._durable_close(
                alloc["id"],
                stale,
                first_pass_accepted=True,
                wall_seconds=1.0,
                repair_count=0,
                verifier_score=None,
                production_outcome="healthy",
            )
        assert ei.value.durable_closed is False
        assert _counts(right, alloc["id"]) == (1, 1)

        # The ordinary retry path then resolves to idempotent success.
        resolved = right.close_allocation(alloc["id"], first_pass_accepted=True, wall_seconds=1.0)
        assert resolved["idempotent"] is True
        assert resolved["outcome_id"] == first["outcome_id"]
        assert _counts(right, alloc["id"]) == (1, 1)
        assert _kinds(learning_log) == ["decision", "outcome"]
    finally:
        left.close()
        right.close()


def test_close_outbox_schema_is_validated(svc: CognitiveBudgetService) -> None:
    """M-47: a degraded outbox table must fail closed, not silently proceed."""
    svc._connection.execute("ALTER TABLE cbm_close_outbox RENAME TO cbm_close_outbox_bak")
    svc._connection.execute("CREATE TABLE cbm_close_outbox (allocation_id TEXT PRIMARY KEY)")
    with pytest.raises(RuntimeError, match="schema mismatch"):
        svc._ensure_close_outbox()
    svc._connection.execute("DROP TABLE cbm_close_outbox")
    svc._connection.execute("ALTER TABLE cbm_close_outbox_bak RENAME TO cbm_close_outbox")


# --- M-47 #2: the learning sink must be durable, not process memory -----------


def test_delivered_learning_is_on_disk_not_in_process_memory(
    svc: CognitiveBudgetService, learning_log: LearningSink
) -> None:
    """M-47: a delivered step must be backed by bytes that outlive the process.

    Delivering to ``learning.api._MEMORY_LOG`` and then setting the outbox
    delivery column is a false durability claim: the marker survives a restart
    and the event does not, so the event is never re-emitted.
    """
    from omniagentos.learning import api as learning_api

    before = list(learning_api._MEMORY_LOG)
    a = svc.allocate(task_id="close-durable-sink")
    result = svc.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=3.0)

    assert result["learning_delivered"] is True
    assert learning_log.path.is_file()
    assert _kinds(learning_log) == ["decision", "outcome"]
    # Nothing was routed to the in-memory log.
    assert list(learning_api._MEMORY_LOG) == before

    row = _outbox(svc, a["id"])
    assert row["log_decision_at"] is not None
    assert row["attach_outcome_at"] is not None


def test_learning_sink_identity_is_derived_from_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-47: a restarted service resolves the same sink for the same database."""
    monkeypatch.delenv("OMNIAGENTOS_CBM_LEARNING_LOG", raising=False)
    db = tmp_path / "runtime" / "cbm-sink.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    first = CognitiveBudgetService(database=str(db))
    try:
        expected = db.parent / "learning" / "cbm_close_learning.jsonl"
        assert first.learning_sink == expected
        a = first.allocate(task_id="close-sink-identity")
        first.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=1.0)
    finally:
        first.close()

    second = CognitiveBudgetService(database=str(db))
    try:
        assert second.learning_sink == first.learning_sink
    finally:
        second.close()

    # The events written by the first service are readable by the second.
    assert _kinds(LearningSink(second.learning_sink)) == ["decision", "outcome"]


def test_unflushed_learning_is_never_marked_delivered(
    svc: CognitiveBudgetService, learning_log: LearningSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-47: the delivery marker must not outrun the durable bytes.

    ``learning.api`` closes the file without fsyncing, so the record is only in
    the page cache when it returns. If the flush fails the step stays pending
    and the close is reported as a retryable failure, not success.
    """
    from omniagentos.cbm import service as cbm_service

    def unflushable(_path: Path) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(cbm_service, "_fsync_sink", unflushable)

    a = svc.allocate(task_id="close-fsync-fail")
    with pytest.raises(CbmCloseRetryableError) as ei:
        svc.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=1.0)
    assert ei.value.durable_closed is True
    assert ei.value.learning_pending == ["log_decision", "attach_outcome"]

    row = _outbox(svc, a["id"])
    assert row["log_decision_at"] is None
    assert row["attach_outcome_at"] is None

    # Once the flush works the pending steps complete and the close succeeds.
    monkeypatch.undo()
    resolved = svc.close_allocation(a["id"], first_pass_accepted=True, wall_seconds=1.0)
    assert resolved["closed"] is True
    assert resolved["idempotent"] is True
    assert _counts(svc, a["id"]) == (1, 1)
    # The unflushed decision is re-emitted, because whether its bytes reached
    # stable storage is genuinely unknown. That duplicate is the documented
    # at-least-once cost of a learning API with no idempotency key; suppressing
    # it would mean trusting an unflushed write, which is the bug being fixed.
    assert _kinds(learning_log) == ["decision", "decision", "outcome"]
    # The business close is still written exactly once.
    assert _outbox(svc, a["id"])["outcome_id"] == resolved["outcome_id"]


# --- M-40: multi-statement writes are all-or-nothing --------------------------


def test_escalate_rolls_back_when_the_allocation_update_fails(
    svc: CognitiveBudgetService,
) -> None:
    """M-40: an escalation audit row never survives a failed rung change.

    Under autocommit the INSERT committed on its own, leaving a permanent
    escalation record for a rung change that never happened.
    """
    a = svc.allocate(task_id="escalate-atomic")
    svc._connection.execute(
        "CREATE TRIGGER block_alloc_update BEFORE UPDATE ON cbm_allocations "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    try:
        with pytest.raises(sqlite3.Error):
            svc.escalate(a["id"], trigger_code="repeated_failure")
    finally:
        svc._connection.execute("DROP TRIGGER block_alloc_update")

    n = svc._connection.execute(
        "SELECT COUNT(*) AS n FROM cbm_escalations WHERE allocation_id = ?", (a["id"],)
    ).fetchone()["n"]
    assert int(n) == 0
    alloc = svc.get_allocation(a["id"])
    assert alloc is not None and alloc["rung"] == a["rung"]

    # The retry is not blocked by leftover state.
    esc = svc.escalate(a["id"], trigger_code="repeated_failure")
    assert esc["from_rung"] == a["rung"]
    assert len(svc.list_escalations(a["id"])) == 1
