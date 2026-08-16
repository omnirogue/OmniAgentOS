"""Multi-process and multi-instance barrier regressions for CBM claim-before-emit (M-47)."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from omniagentos.cbm.service import (
    CbmCloseRetryableError,
    CognitiveBudgetService,
    utc_now_iso,
)


class LearningSink:
    """Reader for the durable close-learning file (M-47)."""

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

    def kinds(self) -> list[str]:
        steps = []
        for row in self.records():
            kind = str(row.get("kind"))
            if kind == "cbm_allocation_close":
                steps.append("decision")
            elif kind == "outcome":
                steps.append("outcome")
            else:
                steps.append(f"unexpected:{kind}")
        return steps


def _mp_close_worker(
    db_path: str,
    sink_path: str,
    alloc_id: str,
    barrier: Any,
    queue: Any,
) -> None:
    """Process worker that waits at a barrier before invoking close_allocation."""
    os.environ["OMNIAGENTOS_CBM_LEARNING_LOG"] = sink_path
    svc = CognitiveBudgetService(database=db_path)
    try:
        barrier.wait(timeout=10.0)
        res = svc.close_allocation(
            alloc_id, first_pass_accepted=True, wall_seconds=5.0, repair_count=0
        )
        queue.put(("ok", res))
    except Exception as exc:  # noqa: BLE001
        queue.put(("err", str(exc)))
    finally:
        svc.close()


def test_multi_process_concurrent_close_barrier(tmp_path: Path) -> None:
    """M-47: N distinct OS processes closing the same allocation simultaneously.

    Claim-before-emit ensures only one process emits each learning step.
    Emitted learning steps must NOT scale with N.
    """
    db_path = str(tmp_path / "cbm-mp-race.db")
    sink_path = tmp_path / "learning" / "sink-mp.jsonl"
    os.environ["OMNIAGENTOS_CBM_LEARNING_LOG"] = str(sink_path)

    # Initialize DB & allocation
    init_svc = CognitiveBudgetService(database=db_path)
    alloc = init_svc.allocate(task_id="mp-race-1")
    alloc_id = str(alloc["id"])
    init_svc.close()

    num_procs = 5
    barrier: Any = mp.Barrier(num_procs)
    queue: Any = mp.Queue()
    procs = [
        mp.Process(
            target=_mp_close_worker,
            args=(db_path, str(sink_path), alloc_id, barrier, queue),
        )
        for _ in range(num_procs)
    ]

    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=15.0)

    results = []
    while not queue.empty():
        results.append(queue.get())

    assert len(results) == num_procs
    for status, data in results:
        assert status == "ok", f"Process failed: {data}"
        assert data["closed"] is True

    sink = LearningSink(sink_path)
    # Crucial assertion for M-47: Exactly 1 decision and 1 outcome event total!
    assert sink.kinds() == ["decision", "outcome"]

    # Business state is singular
    check_svc = CognitiveBudgetService(database=db_path)
    try:
        n_outcomes = check_svc._connection.execute(
            "SELECT COUNT(*) AS n FROM cbm_outcomes WHERE allocation_id = ?",
            (alloc_id,),
        ).fetchone()["n"]
        assert int(n_outcomes) == 1
        n_samples = check_svc._connection.execute(
            "SELECT COALESCE(SUM(samples), 0) AS n FROM cbm_role_stats"
        ).fetchone()["n"]
        assert int(n_samples) == 1
    finally:
        check_svc.close()


def test_multi_instance_concurrent_close_barrier(tmp_path: Path) -> None:
    """M-47: Multiple in-process CognitiveBudgetService instances closing concurrently."""
    db_path = str(tmp_path / "cbm-mi-race.db")
    sink_path = tmp_path / "learning" / "sink-mi.jsonl"
    os.environ["OMNIAGENTOS_CBM_LEARNING_LOG"] = str(sink_path)

    init_svc = CognitiveBudgetService(database=db_path)
    alloc = init_svc.allocate(task_id="mi-race-1")
    alloc_id = str(alloc["id"])
    init_svc.close()

    num_instances = 5

    def _instance_close() -> dict:
        svc = CognitiveBudgetService(database=db_path)
        try:
            return svc.close_allocation(alloc_id, first_pass_accepted=True, wall_seconds=3.0)
        finally:
            svc.close()

    with ThreadPoolExecutor(max_workers=num_instances) as pool:
        futures = [pool.submit(_instance_close) for _ in range(num_instances)]
        results = [f.result(timeout=10.0) for f in futures]

    assert len(results) == num_instances
    for res in results:
        assert res["closed"] is True

    sink = LearningSink(sink_path)
    assert sink.kinds() == ["decision", "outcome"]


def test_stale_claim_recovery(tmp_path: Path) -> None:
    """M-47: A claim left by a crashed/dead process is reclaimed after staleness timeout."""
    db_path = str(tmp_path / "cbm-stale.db")
    sink_path = tmp_path / "learning" / "sink-stale.jsonl"
    os.environ["OMNIAGENTOS_CBM_LEARNING_LOG"] = str(sink_path)

    svc1 = CognitiveBudgetService(database=db_path)
    alloc = svc1.allocate(task_id="stale-1")
    alloc_id = str(alloc["id"])

    # Perform durable business close
    svc1._durable_close(
        alloc_id,
        svc1.get_allocation(alloc_id) or {},
        first_pass_accepted=True,
        wall_seconds=2.0,
        repair_count=0,
        verifier_score=1.0,
        production_outcome="healthy",
    )
    # Inject a stale claim into outbox
    svc1._connection.execute(
        "UPDATE cbm_close_outbox SET log_decision_claim_owner = 'crashed_pid_99999', "
        "log_decision_claimed_at = '2020-01-01T00:00:00Z' WHERE allocation_id = ?",
        (alloc_id,),
    )
    svc1.close()

    # Fresh service instance attempts close
    svc2 = CognitiveBudgetService(database=db_path)
    try:
        res = svc2.close_allocation(alloc_id, first_pass_accepted=True, wall_seconds=2.0)
        assert res["closed"] is True
        assert res["learning_delivered"] is True

        sink = LearningSink(sink_path)
        assert sink.kinds() == ["decision", "outcome"]
    finally:
        svc2.close()


def test_crash_after_claim_before_fsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M-47: Crash after acquiring claim but before fsync recovers without silent loss."""
    monkeypatch.setenv("OMNIAGENTOS_CBM_STALE_CLAIM_TIMEOUT_S", "0.1")
    db_path = str(tmp_path / "cbm-crash-fsync.db")
    sink_path = tmp_path / "learning" / "sink-crash-fsync.jsonl"
    monkeypatch.setenv("OMNIAGENTOS_CBM_LEARNING_LOG", str(sink_path))

    svc1 = CognitiveBudgetService(database=db_path)
    alloc = svc1.allocate(task_id="crash-fsync-1")
    alloc_id = str(alloc["id"])

    # Claim step with owner 'dead_worker'
    svc1._durable_close(
        alloc_id,
        svc1.get_allocation(alloc_id) or {},
        first_pass_accepted=True,
        wall_seconds=1.0,
        repair_count=0,
        verifier_score=1.0,
        production_outcome="healthy",
    )
    svc1._claim_step(alloc_id, "log_decision", "dead_worker", stale_seconds=0.1)
    svc1.close()

    # Wait for claim to become stale
    time.sleep(0.15)

    # New process recovers and delivers
    svc2 = CognitiveBudgetService(database=db_path)
    try:
        res = svc2.close_allocation(alloc_id, first_pass_accepted=True, wall_seconds=1.0)
        assert res["closed"] is True
        sink = LearningSink(sink_path)
        assert sink.kinds() == ["decision", "outcome"]
    finally:
        svc2.close()


def test_crash_after_fsync_before_mark_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-47: Crash after appending/fsyncing but before setting marker re-emits truthfully."""
    from omniagentos.learning import api as learning_api

    monkeypatch.setenv("OMNIAGENTOS_CBM_STALE_CLAIM_TIMEOUT_S", "0.1")
    db_path = str(tmp_path / "cbm-crash-mark.db")
    sink_path = tmp_path / "learning" / "sink-crash-mark.jsonl"
    monkeypatch.setenv("OMNIAGENTOS_CBM_LEARNING_LOG", str(sink_path))

    svc1 = CognitiveBudgetService(database=db_path)
    alloc = svc1.allocate(task_id="crash-mark-1")
    alloc_id = str(alloc["id"])

    outbox_rec = svc1._durable_close(
        alloc_id,
        svc1.get_allocation(alloc_id) or {},
        first_pass_accepted=True,
        wall_seconds=1.0,
        repair_count=0,
        verifier_score=1.0,
        production_outcome="healthy",
    )
    svc1._claim_step(alloc_id, "log_decision", "dead_worker", stale_seconds=0.1)

    # Emit decision and fsync, but crash before _mark_delivered
    decision_payload = json.loads(outbox_rec["decision_payload_json"])
    learning_api.log_decision(decision_payload, path=sink_path)
    svc1.close()

    time.sleep(0.15)

    # Service 2 recovers: re-emits decision (at-least-once) + emits outcome
    svc2 = CognitiveBudgetService(database=db_path)
    try:
        res = svc2.close_allocation(alloc_id, first_pass_accepted=True, wall_seconds=1.0)
        assert res["closed"] is True
        sink = LearningSink(sink_path)
        # Truthful at-least-once re-emission after crash post-fsync
        assert sink.kinds() == ["decision", "decision", "outcome"]
    finally:
        svc2.close()


def test_fresh_foreign_claim_wait_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-47: A fresh foreign claim held throughout attempt loop raises CbmCloseRetryableError."""
    monkeypatch.setenv("OMNIAGENTOS_CBM_STALE_CLAIM_TIMEOUT_S", "30.0")
    db_path = str(tmp_path / "cbm-foreign-claim.db")
    sink_path = tmp_path / "learning" / "sink-foreign.jsonl"
    monkeypatch.setenv("OMNIAGENTOS_CBM_LEARNING_LOG", str(sink_path))

    svc = CognitiveBudgetService(database=db_path)
    try:
        alloc = svc.allocate(task_id="foreign-claim-1")
        alloc_id = str(alloc["id"])
        svc._durable_close(
            alloc_id,
            svc.get_allocation(alloc_id) or {},
            first_pass_accepted=True,
            wall_seconds=1.0,
            repair_count=0,
            verifier_score=1.0,
            production_outcome="healthy",
        )
        # Inject fresh foreign claim for log_decision
        now = utc_now_iso()
        svc._connection.execute(
            "UPDATE cbm_close_outbox SET log_decision_claim_owner = 'foreign_worker_123', "
            "log_decision_claimed_at = ? WHERE allocation_id = ?",
            (now, alloc_id),
        )

        with pytest.raises(CbmCloseRetryableError) as exc_info:
            svc.close_allocation(alloc_id, first_pass_accepted=True)

        err = exc_info.value
        assert err.durable_closed is True
        assert err.learning_pending == ["log_decision", "attach_outcome"]
    finally:
        svc.close()


def test_missing_outbox_raises_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M-47: Missing outbox row during delivery raises CbmCloseRetryableError."""
    db_path = str(tmp_path / "cbm-missing-outbox.db")
    sink_path = tmp_path / "learning" / "sink-missing-outbox.jsonl"
    monkeypatch.setenv("OMNIAGENTOS_CBM_LEARNING_LOG", str(sink_path))

    svc = CognitiveBudgetService(database=db_path)
    try:
        alloc = svc.allocate(task_id="missing-outbox-1")
        alloc_id = str(alloc["id"])
        record = svc._durable_close(
            alloc_id,
            svc.get_allocation(alloc_id) or {},
            first_pass_accepted=True,
            wall_seconds=1.0,
            repair_count=0,
            verifier_score=1.0,
            production_outcome="healthy",
        )
        # Delete the outbox row
        svc._connection.execute("DELETE FROM cbm_close_outbox WHERE allocation_id = ?", (alloc_id,))

        with pytest.raises(CbmCloseRetryableError) as exc_info:
            svc._deliver_close_learning(record, idempotent=False)

        err = exc_info.value
        assert err.durable_closed is True
        assert err.learning_pending == ["log_decision", "attach_outcome"]
    finally:
        svc.close()


def test_partial_delivery_raises_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M-47: Partial delivery (step 1 delivered, step 2 foreign claimed) raises CbmCloseRetryableError."""
    monkeypatch.setenv("OMNIAGENTOS_CBM_STALE_CLAIM_TIMEOUT_S", "30.0")
    db_path = str(tmp_path / "cbm-partial-delivery.db")
    sink_path = tmp_path / "learning" / "sink-partial.jsonl"
    monkeypatch.setenv("OMNIAGENTOS_CBM_LEARNING_LOG", str(sink_path))

    svc = CognitiveBudgetService(database=db_path)
    try:
        alloc = svc.allocate(task_id="partial-delivery-1")
        alloc_id = str(alloc["id"])
        svc._durable_close(
            alloc_id,
            svc.get_allocation(alloc_id) or {},
            first_pass_accepted=True,
            wall_seconds=1.0,
            repair_count=0,
            verifier_score=1.0,
            production_outcome="healthy",
        )
        # Mark log_decision as delivered, but foreign claim attach_outcome
        now = utc_now_iso()
        svc._connection.execute(
            "UPDATE cbm_close_outbox SET log_decision_at = ?, "
            "attach_outcome_claim_owner = 'foreign_worker_456', "
            "attach_outcome_claimed_at = ? WHERE allocation_id = ?",
            (now, now, alloc_id),
        )

        with pytest.raises(CbmCloseRetryableError) as exc_info:
            svc.close_allocation(alloc_id, first_pass_accepted=True)

        err = exc_info.value
        assert err.durable_closed is True
        assert err.learning_pending == ["attach_outcome"]
    finally:
        svc.close()


def test_claim_owner_mismatch_raises_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M-47: Claim owner mismatch during _mark_delivered raises CbmCloseRetryableError."""
    db_path = str(tmp_path / "cbm-owner-mismatch.db")
    sink_path = tmp_path / "learning" / "sink-mismatch.jsonl"
    monkeypatch.setenv("OMNIAGENTOS_CBM_LEARNING_LOG", str(sink_path))

    svc = CognitiveBudgetService(database=db_path)
    try:
        alloc = svc.allocate(task_id="mismatch-1")
        alloc_id = str(alloc["id"])
        record = svc._durable_close(
            alloc_id,
            svc.get_allocation(alloc_id) or {},
            first_pass_accepted=True,
            wall_seconds=1.0,
            repair_count=0,
            verifier_score=1.0,
            production_outcome="healthy",
        )
        # Claim under owner1
        svc._claim_step(alloc_id, "log_decision", "owner1", stale_seconds=30.0)

        # Alter owner in DB to owner2
        svc._connection.execute(
            "UPDATE cbm_close_outbox SET log_decision_claim_owner = 'owner2' "
            "WHERE allocation_id = ?",
            (alloc_id,),
        )

        # Calling _mark_delivered with owner1 must return False (0 rows updated)
        marked = svc._mark_delivered(alloc_id, "log_decision", "owner1")
        assert marked is False

        # Attempting close_allocation under this state raises CbmCloseRetryableError
        with pytest.raises(CbmCloseRetryableError) as exc_info:
            svc._deliver_close_learning(record, idempotent=False)

        err = exc_info.value
        assert err.durable_closed is True
        assert "log_decision" in err.learning_pending
    finally:
        svc.close()


def test_post_loop_pending_state_raises_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-47: Post-loop outbox verification catches any pending steps and raises CbmCloseRetryableError."""
    db_path = str(tmp_path / "cbm-post-loop.db")
    sink_path = tmp_path / "learning" / "sink-post-loop.jsonl"
    monkeypatch.setenv("OMNIAGENTOS_CBM_LEARNING_LOG", str(sink_path))

    svc = CognitiveBudgetService(database=db_path)
    try:
        alloc = svc.allocate(task_id="post-loop-1")
        alloc_id = str(alloc["id"])
        record = svc._durable_close(
            alloc_id,
            svc.get_allocation(alloc_id) or {},
            first_pass_accepted=True,
            wall_seconds=1.0,
            repair_count=0,
            verifier_score=1.0,
            production_outcome="healthy",
        )

        # Intercept _claim_step to do nothing and return False, forcing loop to exhaust
        monkeypatch.setattr(svc, "_claim_step", lambda *args, **kwargs: False)

        with pytest.raises(CbmCloseRetryableError) as exc_info:
            svc._deliver_close_learning(record, idempotent=False)

        err = exc_info.value
        assert err.durable_closed is True
        assert err.learning_pending == ["log_decision", "attach_outcome"]
    finally:
        svc.close()
