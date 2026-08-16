"""Unit and integration tests for the durable job queue."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import httpx
import pytest

from omniagentos.api.main import app
from omniagentos.lab import jobs
from omniagentos.lab.api.deps import get_lab_store
from omniagentos.lab.contracts import (
    Budgets,
    Disposition,
    Experiment,
    ExperimentStatus,
    Scorecard,
    SurfaceKind,
)
from omniagentos.lab.db import LabJobLeaseLost, LabStore
from tests.support.db_template import migrated_db


class _Crash(BaseException):
    """Simulated worker crash."""


@pytest.fixture
def fresh_store_path(tmp_path: Any) -> str:
    # A pre-migrated template copy: the same schema LabStore would build, minus
    # the 86 per-test migration applies. ``test_migration_085_applies_to_a_
    # fresh_database`` below deliberately does NOT use this — it has to migrate
    # for real.
    return migrated_db(LabStore, tmp_path / "lab.db")


@pytest.fixture
def store(fresh_store_path: str) -> LabStore:
    return LabStore(fresh_store_path)


# 1. test_duplicate_submission_is_deduplicated_by_idempotency_key
def test_duplicate_submission_is_deduplicated_by_idempotency_key(store: LabStore) -> None:
    now = "2026-07-27T12:00:00Z"
    job_1, created_1 = jobs.enqueue(
        store, "exp_1", idempotency_key="idem_key", dry_run=False, now=now
    )
    assert created_1 is True
    assert job_1.job_id.startswith("labjob_")
    assert job_1.idempotency_key == "idem_key"
    assert job_1.state == jobs.QUEUED

    # Repeat submission
    job_2, created_2 = jobs.enqueue(
        store, "exp_1", idempotency_key="idem_key", dry_run=False, now=now
    )
    assert created_2 is False
    assert job_2.job_id == job_1.job_id

    # Check total rows in database
    cursor = store._connection.execute("SELECT COUNT(*) FROM lab_jobs")
    assert cursor.fetchone()[0] == 1

    # Claim the job and make sure repeat enqueue does not modify running state / attempt count
    job_claimed = jobs.claim(store, owner="worker_1", now="2026-07-27T12:01:00Z")
    assert job_claimed is not None
    assert job_claimed.state == jobs.RUNNING
    assert job_claimed.attempt == 1

    job_3, created_3 = jobs.enqueue(
        store, "exp_1", idempotency_key="idem_key", dry_run=False, now=now
    )
    assert created_3 is False
    assert job_3.state == jobs.RUNNING
    assert job_3.attempt == 1


# 2. test_route_enqueues_and_does_not_execute
def test_route_enqueues_and_does_not_execute(
    fresh_store_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LabStore(fresh_store_path)

    # Seed the experiment
    experiment = Experiment(
        id="exp_seed",
        hypothesis="Try a focused prompt",
        discipline="writing",
        mutable_surface_kind=SurfaceKind.PROMPT,
        champion_surface_id="srf_champion",
        challenger_surface_id="srf_challenger",
        eval_suite_id="evs_writing",
        primary_metric="accuracy",
        budgets=Budgets(replicates=2),
        status=ExperimentStatus.DECIDED,
        disposition=Disposition.PROMOTE,
        scorecard=Scorecard(audit_flags=[], safety_regression=False),
        created_at="2026-01-03T00:00:00Z",
        updated_at="2026-01-03T00:00:00Z",
    )
    store.create_experiment(experiment)

    # Override get_lab_store dependency on FastAPI app
    app.dependency_overrides[get_lab_store] = lambda: store

    campaign_called = False

    def fake_run_experiment(*args: Any, **kwargs: Any) -> Any:
        nonlocal campaign_called
        campaign_called = True
        raise AssertionError("campaign must not run in the request")

    monkeypatch.setattr("omniagentos.lab.campaign.run_experiment", fake_run_experiment)

    # Drive FastAPI client
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        response = asyncio.run(
            client.post(
                "/api/lab/experiments/exp_seed/run",
                json={"dry_run": True},
                headers={"Idempotency-Key": "route_idem_key"},
            )
        )
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "queued"
        assert data["job_id"] is not None
        assert data["idempotency_key"] == "route_idem_key"
        assert data["state"] == "queued"

        # Verify job actually exists in state 'queued' in database
        job = store.get_job(data["job_id"])
        assert job is not None
        assert job["state"] == "queued"
        assert campaign_called is False

    finally:
        app.dependency_overrides.clear()


# 3. test_claim_takes_lease_and_second_claim_is_blocked
def test_claim_takes_lease_and_second_claim_is_blocked(store: LabStore) -> None:
    now = "2026-07-27T12:00:00Z"
    job, _ = jobs.enqueue(store, "exp_1", idempotency_key="key", now=now)

    job1 = jobs.claim(store, owner="worker_1", now=now)
    assert job1 is not None
    assert job1.job_id == job.job_id
    assert job1.lease_owner == "worker_1"
    assert job1.lease_generation == 1
    assert job1.attempt == 1
    assert job1.state == jobs.RUNNING

    job2 = jobs.claim(store, owner="worker_2", now=now)
    assert job2 is None


# 4. test_expired_lease_is_adopted_exactly_once
def test_expired_lease_is_adopted_exactly_once(store: LabStore) -> None:
    now = "2026-07-27T12:00:00Z"
    job, _ = jobs.enqueue(store, "exp_1", idempotency_key="key", now=now)

    job_claimed = jobs.claim(store, owner="worker_1", now=now)
    assert job_claimed is not None

    # Reconcile early: not expired
    exp_early = jobs.reconcile(store, now="2026-07-27T12:04:00Z")
    assert len(exp_early) == 0

    # Reconcile after expiry (lease is 300s, so 301s later is expired)
    now_expired = "2026-07-27T12:06:00Z"
    exp_late = jobs.reconcile(store, now=now_expired)
    assert len(exp_late) == 1
    assert exp_late[0].job_id == job.job_id
    assert exp_late[0].lease_owner == "worker_1"
    assert exp_late[0].attempt == 1

    # Simulate racing adopters
    # Call claim_job twice with the same stale generation semantics
    # First claim succeeds and updates lease_generation to 2
    claim_1 = store.claim_job(owner="worker_2", now=now_expired, lease_expires_at="2026-07-27T12:11:00Z")
    assert claim_1 is not None
    assert claim_1["lease_generation"] == 2
    assert claim_1["attempt"] == 2

    # Second claim fails because the job was adopted (lease_generation changed / not matching anymore)
    claim_2 = store.claim_job(owner="worker_3", now=now_expired, lease_expires_at="2026-07-27T12:11:00Z")
    assert claim_2 is None

    # Original worker heartbeat fails (fenced write after adoption)
    with pytest.raises(LabJobLeaseLost):
        store.heartbeat_job(
            job_id=job.job_id,
            owner="worker_1",
            lease_generation=1,
            lease_expires_at="2026-07-27T12:11:00Z",
            now=now_expired,
        )


# 5. test_crash_at_each_state_recovers
@pytest.mark.parametrize(
    "crash_point",
    [
        "before_claim",
        "before_stage",
        "mid_stage",
        "after_stage_before_next",
    ],
)
def test_crash_at_each_state_recovers(store: LabStore, crash_point: str) -> None:
    now = "2026-07-27T12:00:00Z"
    job, _ = jobs.enqueue(store, "exp_1", idempotency_key="key", now=now)

    calls = {"one": 0, "two": 0, "three": 0}

    def run_stage_one(j: jobs.LabJob, d: dict[str, Any]) -> dict[str, Any]:
        calls["one"] += 1
        return {"val_1": 42}

    def run_stage_two(j: jobs.LabJob, d: dict[str, Any]) -> dict[str, Any]:
        calls["two"] += 1
        if crash_point == "mid_stage" and calls["two"] == 1:
            raise _Crash("worker crashed")
        return {"val_2": 84}

    def run_stage_three(j: jobs.LabJob, d: dict[str, Any]) -> dict[str, Any]:
        calls["three"] += 1
        return {"val_3": 126}

    pipeline = [
        jobs.JobStage("one", run_stage_one),
        jobs.JobStage("two", run_stage_two),
        jobs.JobStage("three", run_stage_three),
    ]

    # Handle crash injection/points
    if crash_point == "before_claim":
        # First worker never claims, second worker claims and drives to success
        pass
    elif crash_point == "before_stage":
        # Claimed but immediately crashes before execute
        j = jobs.claim(store, owner="worker_1", now=now)
        assert j is not None
    elif crash_point == "mid_stage":
        # Claims and executes, but stage two raises _Crash
        j = jobs.claim(store, owner="worker_1", now=now)
        assert j is not None
        with pytest.raises(_Crash):
            jobs.execute(store, j, pipeline, owner="worker_1", now_fn=lambda: now)
    elif crash_point == "after_stage_before_next":
        # Claim, run stage 1, checkpoint written, then crashes
        j = jobs.claim(store, owner="worker_1", now=now)
        assert j is not None
        # Manually run stage 1 and checkpoint to simulate crash after stage 1's checkpoint
        now_stage_1 = "2026-07-27T12:01:00Z"
        res = run_stage_one(j, {})
        store.checkpoint_job(
            job_id=j.job_id,
            owner="worker_1",
            lease_generation=j.lease_generation,
            checkpoint={"completed": ["one"], "data": res},
            lease_expires_at="2026-07-27T12:06:00Z",
            now=now_stage_1,
        )

    # Let time pass past lease expiration (300s)
    now_adopt = "2026-07-27T12:07:00Z"
    j_adopt = jobs.claim(store, owner="worker_2", now=now_adopt)
    assert j_adopt is not None
    assert j_adopt.attempt > 1 if crash_point != "before_claim" else j_adopt.attempt == 1

    # Second worker successfully executes the pipeline to completion
    final_job = jobs.execute(store, j_adopt, pipeline, owner="worker_2", now_fn=lambda: now_adopt)
    assert final_job.state == jobs.SUCCEEDED
    assert final_job.result is not None
    assert final_job.result["stages"] == ["one", "two", "three"]
    assert final_job.result["data"] == {"val_1": 42, "val_2": 84, "val_3": 126}

    # Verify stage counters:
    if crash_point == "before_claim" or crash_point == "before_stage":
        assert calls == {"one": 1, "two": 1, "three": 1}
    elif crash_point == "mid_stage":
        # stage 1 ran once by first worker (persisted). Stage 2 started but crashed.
        # Worker 2 adopted, skipped stage 1, ran stage 2 (2nd try), then stage 3.
        assert calls == {"one": 1, "two": 2, "three": 1}
    elif crash_point == "after_stage_before_next":
        # stage 1 ran by first worker (persisted). Stage 2 and 3 ran by worker 2.
        assert calls == {"one": 1, "two": 1, "three": 1}


# 6. test_resume_continues_from_checkpoint
def test_resume_continues_from_checkpoint(store: LabStore) -> None:
    now = "2026-07-27T12:00:00Z"
    job, _ = jobs.enqueue(store, "exp_1", idempotency_key="key", now=now)

    calls = {"one": 0, "two": 0, "three": 0}

    def run_stage_one(j: jobs.LabJob, d: dict[str, Any]) -> dict[str, Any]:
        calls["one"] += 1
        return {"val_1": 10}

    def run_stage_two(j: jobs.LabJob, d: dict[str, Any]) -> dict[str, Any]:
        calls["two"] += 1
        if calls["two"] == 1:
            raise _Crash("crashed mid-stage 2")
        return {"val_2": 20}

    def run_stage_three(j: jobs.LabJob, d: dict[str, Any]) -> dict[str, Any]:
        calls["three"] += 1
        return {"val_3": 30}

    pipeline = [
        jobs.JobStage("one", run_stage_one),
        jobs.JobStage("two", run_stage_two),
        jobs.JobStage("three", run_stage_three),
    ]

    j = jobs.claim(store, owner="worker_1", now=now)
    assert j is not None
    with pytest.raises(_Crash):
        jobs.execute(store, j, pipeline, owner="worker_1", now_fn=lambda: now)

    # Adopt after lease expiration
    now_adopt = "2026-07-27T12:07:00Z"
    j_adopt = jobs.claim(store, owner="worker_2", now=now_adopt)
    assert j_adopt is not None

    final_job = jobs.execute(store, j_adopt, pipeline, owner="worker_2", now_fn=lambda: now_adopt)
    assert final_job.state == jobs.SUCCEEDED
    assert final_job.result is not None
    assert final_job.result["stages"] == ["one", "two", "three"]
    assert calls == {"one": 1, "two": 2, "three": 1}


# 7. test_cancel_before_claim_is_immediately_terminal
def test_cancel_before_claim_is_immediately_terminal(store: LabStore) -> None:
    now = "2026-07-27T12:00:00Z"
    job, _ = jobs.enqueue(store, "exp_1", idempotency_key="key", now=now)

    cancelled_job = jobs.request_cancel(store, job.job_id, now=now)
    assert cancelled_job is not None
    assert cancelled_job.state == jobs.CANCELLED
    assert cancelled_job.cancel_requested is True
    assert cancelled_job.finished_at == now
    assert cancelled_job.error == "cancelled before start"

    # Subsequent claim returns None
    claimed_job = jobs.claim(store, owner="worker_1", now=now)
    assert claimed_job is None


# 8. test_cancel_during_run_is_honored_before_next_stage
def test_cancel_during_run_is_honored_before_next_stage(store: LabStore) -> None:
    now = "2026-07-27T12:00:00Z"
    job, _ = jobs.enqueue(store, "exp_1", idempotency_key="key", now=now)

    calls = {"one": 0, "two": 0}

    def run_stage_one(j: jobs.LabJob, d: dict[str, Any]) -> dict[str, Any]:
        calls["one"] += 1
        # Request cancel during execution of stage one
        store.request_job_cancel(job_id=j.job_id, now=now)
        return {"val_1": 100}

    def run_stage_two(j: jobs.LabJob, d: dict[str, Any]) -> dict[str, Any]:
        calls["two"] += 1
        return {"val_2": 200}

    pipeline = [
        jobs.JobStage("one", run_stage_one),
        jobs.JobStage("two", run_stage_two),
    ]

    j_claimed = jobs.claim(store, owner="worker_1", now=now)
    assert j_claimed is not None

    final_job = jobs.execute(store, j_claimed, pipeline, owner="worker_1", now_fn=lambda: now)
    assert final_job.state == jobs.CANCELLED
    assert final_job.error == "cancelled before stage two"
    assert calls == {"one": 1, "two": 0}
    # Checkpoint records stage one
    assert final_job.checkpoint["completed"] == ["one"]


# 9. test_fenced_write_after_adoption_raises
def test_fenced_write_after_adoption_raises(store: LabStore) -> None:
    now = "2026-07-27T12:00:00Z"
    job, _ = jobs.enqueue(store, "exp_1", idempotency_key="key", now=now)

    j_w1 = jobs.claim(store, owner="worker_1", now=now)
    assert j_w1 is not None

    # Adopt by worker 2
    now_adopt = "2026-07-27T12:07:00Z"
    j_w2 = jobs.claim(store, owner="worker_2", now=now_adopt)
    assert j_w2 is not None

    # Worker 1 tries to checkpoint or finish, both should raise LabJobLeaseLost
    with pytest.raises(LabJobLeaseLost):
        store.checkpoint_job(
            job_id=job.job_id,
            owner="worker_1",
            lease_generation=1,
            checkpoint={"completed": ["one"], "data": {}},
            lease_expires_at="2026-07-27T12:12:00Z",
            now=now_adopt,
        )

    with pytest.raises(LabJobLeaseLost):
        store.finish_job(
            job_id=job.job_id,
            owner="worker_1",
            lease_generation=1,
            state=jobs.SUCCEEDED,
            result={},
            error=None,
            now=now_adopt,
        )


# 10. test_failed_stage_records_error_and_preserves_checkpoint
def test_failed_stage_records_error_and_preserves_checkpoint(store: LabStore) -> None:
    now = "2026-07-27T12:00:00Z"
    job, _ = jobs.enqueue(store, "exp_1", idempotency_key="key", now=now)

    def run_stage_one(j: jobs.LabJob, d: dict[str, Any]) -> dict[str, Any]:
        return {"val_1": 50}

    def run_stage_two(j: jobs.LabJob, d: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("stage two failed dramatically")

    pipeline = [
        jobs.JobStage("one", run_stage_one),
        jobs.JobStage("two", run_stage_two),
    ]

    j_claimed = jobs.claim(store, owner="worker_1", now=now)
    assert j_claimed is not None

    final_job = jobs.execute(store, j_claimed, pipeline, owner="worker_1", now_fn=lambda: now)
    assert final_job.state == jobs.FAILED
    assert final_job.error is not None
    assert "ValueError: stage two failed dramatically" in final_job.error
    # checkpoint is preserved
    assert final_job.checkpoint["completed"] == ["one"]
    assert final_job.checkpoint["data"] == {"val_1": 50}

    # Subsequent claim is blocked
    j_claim_stale = jobs.claim(store, owner="worker_2", now="2026-07-27T12:10:00Z")
    assert j_claim_stale is None

    # New submission with different key creates new job
    job_new, created_new = jobs.enqueue(store, "exp_1", idempotency_key="key_new", now=now)
    assert created_new is True
    assert job_new.job_id != job.job_id


# 11. test_terminal_job_is_never_reclaimed
def test_terminal_job_is_never_reclaimed(store: LabStore) -> None:
    now = "2026-07-27T12:00:00Z"
    # Succeeded
    job_suc, _ = jobs.enqueue(store, "exp_1", idempotency_key="key_suc", now=now)
    j_suc = jobs.claim(store, owner="worker_1", now=now)
    assert j_suc is not None
    store.finish_job(
        job_id=job_suc.job_id,
        owner="worker_1",
        lease_generation=j_suc.lease_generation,
        state=jobs.SUCCEEDED,
        result={},
        error=None,
        now=now,
    )

    # Failed
    job_fail, _ = jobs.enqueue(store, "exp_1", idempotency_key="key_fail", now=now)
    j_fail = jobs.claim(store, owner="worker_1", now=now)
    assert j_fail is not None
    store.finish_job(
        job_id=job_fail.job_id,
        owner="worker_1",
        lease_generation=j_fail.lease_generation,
        state=jobs.FAILED,
        result={},
        error="broken",
        now=now,
    )

    # Cancelled
    job_can, _ = jobs.enqueue(store, "exp_1", idempotency_key="key_can", now=now)
    jobs.request_cancel(store, job_can.job_id, now=now)

    # Advance time far in future past lease
    claimed = jobs.claim(store, owner="worker_2", now="2026-07-27T13:00:00Z")
    assert claimed is None


# 12. test_migration_085_applies_to_a_fresh_database
def test_migration_085_applies_to_a_fresh_database(tmp_path: Any) -> None:
    fresh_db_path = str(tmp_path / "fresh.db")
    # Instantiating LabStore triggers SqliteStore and automigrates
    store = LabStore(fresh_db_path)

    # 1. Assert schema version is >= 85
    cursor = store._connection.execute("SELECT version FROM schema_migrations")
    versions = {row[0] for row in cursor.fetchall()}
    assert 85 in versions

    # 2. Assert lab_jobs table exists in sqlite_master
    cursor = store._connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'lab_jobs'"
    )
    assert cursor.fetchone() is not None

    # 3. Assert index exists on idx_lab_jobs_claimable
    cursor = store._connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_lab_jobs_claimable'"
    )
    assert cursor.fetchone() is not None

    # 4. Assert UNIQUE constraint on idempotency_key is enforced
    # Write a test row manually
    store._connection.execute(
        "INSERT INTO lab_jobs "
        "(job_id, idempotency_key, experiment_id, state, dry_run, created_at, updated_at) "
        "VALUES ('j1', 'idem_key_man', 'exp_x', 'queued', 0, 'now', 'now')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            "INSERT INTO lab_jobs "
            "(job_id, idempotency_key, experiment_id, state, dry_run, created_at, updated_at) "
            "VALUES ('j2', 'idem_key_man', 'exp_y', 'queued', 0, 'now', 'now')"
        )


def test_cancelled_job_with_dead_worker_is_adopted_and_finalized(store: LabStore) -> None:
    now = "2026-07-27T12:00:00Z"
    job, _ = jobs.enqueue(store, "exp_1", idempotency_key="key", now=now)

    claimed_1 = jobs.claim(store, owner="worker_1", now=now)
    assert claimed_1 is not None

    # Request cancel while worker_1 has it, then abandon worker_1
    cancelled_job = jobs.request_cancel(store, job.job_id, now=now)
    assert cancelled_job is not None
    assert cancelled_job.cancel_requested is True
    assert cancelled_job.state == jobs.RUNNING

    calls = {"n": 0}
    def run_stage(j: jobs.LabJob, d: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        return {}
    pipeline = [jobs.JobStage("stage_1", run_stage)]

    # Advance past the lease
    now_adopt = "2026-07-27T12:10:00Z"
    res_job = jobs.run_once(store, pipeline, owner="worker_2", now_fn=lambda: now_adopt)
    assert res_job is not None
    assert res_job.state == jobs.CANCELLED
    assert res_job.finished_at == now_adopt
    assert calls["n"] == 0

    further_claim = jobs.claim(store, owner="worker_3", now=now_adopt)
    assert further_claim is None


def test_heartbeat_extends_the_lease_and_blocks_adoption(store: LabStore) -> None:
    t0 = "2026-07-27T12:00:00Z"
    t0_plus_240 = "2026-07-27T12:04:00Z"
    t0_plus_400 = "2026-07-27T12:06:40Z"
    t0_plus_600 = "2026-07-27T12:10:00Z"

    # Enqueue and claim job_1
    job_1, _ = jobs.enqueue(store, "exp_1", idempotency_key="key_1", now=t0)
    claimed_1 = jobs.claim(store, owner="worker_1", now=t0)
    assert claimed_1 is not None

    # Heartbeat job_1 at T0 + 240
    refreshed_1 = jobs.heartbeat(store, claimed_1, owner="worker_1", now=t0_plus_240)
    assert refreshed_1.lease_expires_at == "2026-07-27T12:09:00Z"

    # At T0 + 400: claim and reconcile report nothing for job_1
    claimed_2 = jobs.claim(store, owner="worker_2", now=t0_plus_400)
    assert claimed_2 is None
    expired = jobs.reconcile(store, now=t0_plus_400)
    assert len(expired) == 0

    # Prove that WITHOUT the heartbeat the same instant WOULD have been adoptable
    # Enqueue and claim job_2 at T0 (no heartbeat)
    job_2, _ = jobs.enqueue(store, "exp_1", idempotency_key="key_2", now=t0)
    claimed_2_1 = jobs.claim(store, owner="worker_1", now=t0)
    assert claimed_2_1 is not None

    # At T0 + 400, claiming job_2 should succeed because its lease expired at T0 + 300
    claimed_2_2 = jobs.claim(store, owner="worker_2", now=t0_plus_400)
    assert claimed_2_2 is not None
    assert claimed_2_2.job_id == job_2.job_id

    # At T0 + 600, job_1 is now past its extended lease expiry (T0 + 540)
    # Let's verify claiming it now succeeds
    claimed_1_2 = jobs.claim(store, owner="worker_2", now=t0_plus_600)
    assert claimed_1_2 is not None
    assert claimed_1_2.job_id == job_1.job_id

    # Also assert jobs.heartbeat raises LabJobLeaseLost when called with a job whose lease was already adopted
    with pytest.raises(LabJobLeaseLost):
        jobs.heartbeat(store, refreshed_1, owner="worker_1", now=t0_plus_600)


def test_campaign_stage_runs_once_and_is_not_repeated_after_adoption(
    store: LabStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def fake_run_experiment(store_arg: Any, evaluator_arg: Any, experiment_id: str, dry_run: bool) -> Disposition:
        calls["n"] += 1
        return Disposition.PROMOTE

    monkeypatch.setattr("omniagentos.lab.campaign.run_experiment", fake_run_experiment)

    t0 = "2026-07-27T12:00:00Z"
    job, _ = jobs.enqueue(store, "exp_1", idempotency_key="key_campaign", now=t0)

    stages = jobs.campaign_stages(store, evaluator=object())

    # Worker 1 claims job
    j_w1 = jobs.claim(store, owner="worker_1", now=t0)
    assert j_w1 is not None

    # Run the stage by hand
    res = stages[0].run(j_w1, {})
    assert res == {"disposition": "promote"}

    # Checkpoint the job manually as if worker 1 wrote it but crashed before finishing
    store.checkpoint_job(
        job_id=j_w1.job_id,
        owner="worker_1",
        lease_generation=j_w1.lease_generation,
        checkpoint={"completed": ["campaign"], "data": {"disposition": "promote"}},
        lease_expires_at="2026-07-27T12:05:00Z",
        now=t0,
    )

    # Worker 1 is abandoned. Advance past the lease
    now_adopt = "2026-07-27T12:10:00Z"
    final_job = jobs.run_once(store, stages, owner="worker_2", now_fn=lambda: now_adopt)

    assert final_job is not None
    assert final_job.state == jobs.SUCCEEDED
    assert final_job.result is not None
    assert final_job.result["stages"] == ["campaign"]
    assert final_job.result["data"]["disposition"] == "promote"
    assert calls["n"] == 1





class TestDrainRoutineIsRegistered:
    """Lane 11 shipped a durable queue with nothing draining it — /run enqueued
    and returned 202, and the job sat forever. These pin the wiring."""

    def test_drain_routine_payload_is_valid(self) -> None:
        from omniagentos.scheduler.routines import lab_jobs_drain_routine, validate_routine

        payload = lab_jobs_drain_routine()
        validate_routine(payload)
        assert payload["trigger_type"] == "cron"
        assert payload["status"] == "active"

    def test_ensure_registers_once_and_is_idempotent(self, tmp_path) -> None:
        from omniagentos.db.store import SqliteStore
        from omniagentos.lab.jobs import ensure_lab_jobs_routine
        from omniagentos.scheduler.routines import LAB_JOBS_ROUTINE_NAME
        from omniagentos.scheduler.store import RoutinesStore

        store = SqliteStore(str(tmp_path / "r.db"))
        first = ensure_lab_jobs_routine(store)
        second = ensure_lab_jobs_routine(store)
        assert first["name"] == LAB_JOBS_ROUTINE_NAME
        assert second["id"] == first["id"], "a second call must not create a duplicate routine"

        names = [r["name"] for r in RoutinesStore(store).list_routines()]
        assert names.count(LAB_JOBS_ROUTINE_NAME) == 1
