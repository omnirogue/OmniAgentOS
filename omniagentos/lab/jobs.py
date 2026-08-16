"""Durable job engine for lab experiment runs.

This module manages the lifecycle, execution, and fencing of lab jobs.
The HTTP routes in the API enqueue a run by creating an entry in state 'queued'
with an idempotency key to prevent duplicate runs.
A background worker (or periodic loop) claims queued or timed-out jobs, taking
a fenced lease on them.
During execution, the job progresses through discrete, checkpointed stages.
Each stage checkpoint is persisted to disk before moving on.
If a worker crashes mid-run, another worker will reclaim the job once the lease
expires, resuming work precisely from the last successful checkpoint instead of
re-running (and re-paying for) already completed steps.

Contract:
A stage must either complete within lease_seconds or call heartbeat while it runs;
otherwise the lease expires mid-stage, another worker adopts the job, and the
stage's paid work is performed twice. This is the one rule a stage author has
to honor.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from omniagentos.contracts import utc_now_iso
from omniagentos.lab.db import LabJobLeaseLost

# State constants
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

NON_TERMINAL_JOB_STATES: tuple[str, ...] = (QUEUED, RUNNING)
TERMINAL_JOB_STATES: tuple[str, ...] = (SUCCEEDED, FAILED, CANCELLED)
LEASE_SECONDS = 300


@dataclass(frozen=True)
class LabJob:
    job_id: str
    idempotency_key: str
    experiment_id: str
    state: str
    dry_run: bool
    attempt: int
    lease_owner: str | None
    lease_expires_at: str | None
    lease_generation: int
    cancel_requested: bool
    checkpoint: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    created_at: str
    updated_at: str
    finished_at: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> LabJob:
        checkpoint = {}
        if row.get("checkpoint_json"):
            checkpoint = json.loads(row["checkpoint_json"])

        result = None
        if row.get("result_json"):
            result = json.loads(row["result_json"])

        return cls(
            job_id=row["job_id"],
            idempotency_key=row["idempotency_key"],
            experiment_id=row["experiment_id"],
            state=row["state"],
            dry_run=bool(row["dry_run"]),
            attempt=row["attempt"],
            lease_owner=row.get("lease_owner"),
            lease_expires_at=row.get("lease_expires_at"),
            lease_generation=row["lease_generation"],
            cancel_requested=bool(row["cancel_requested"]),
            checkpoint=checkpoint,
            result=result,
            error=row.get("error"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            finished_at=row.get("finished_at"),
        )


class JobCancelled(RuntimeError):
    """Raised inside the executor when a cancellation request is observed."""


@dataclass(frozen=True)
class JobStage:
    name: str
    run: Callable[[LabJob, dict[str, Any]], Mapping[str, Any] | None]


@dataclass(frozen=True)
class LeaseExpiry:
    job_id: str
    lease_owner: str | None
    expired_at: str | None
    attempt: int


class JobStore(Protocol):
    def enqueue_job(
        self,
        *,
        job_id: str,
        idempotency_key: str,
        experiment_id: str,
        dry_run: bool,
        now: str,
    ) -> tuple[dict[str, Any], bool]: ...

    def get_job(self, job_id: str) -> dict[str, Any] | None: ...

    def get_job_by_key(self, idempotency_key: str) -> dict[str, Any] | None: ...

    def list_jobs(
        self,
        experiment_id: str | None = None,
        state: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...

    def claim_job(
        self, *, owner: str, now: str, lease_expires_at: str
    ) -> dict[str, Any] | None: ...

    def heartbeat_job(
        self,
        *,
        job_id: str,
        owner: str,
        lease_generation: int,
        lease_expires_at: str,
        now: str,
    ) -> dict[str, Any]: ...

    def checkpoint_job(
        self,
        *,
        job_id: str,
        owner: str,
        lease_generation: int,
        checkpoint: dict[str, Any],
        lease_expires_at: str,
        now: str,
    ) -> dict[str, Any]: ...

    def request_job_cancel(self, *, job_id: str, now: str) -> dict[str, Any] | None: ...

    def finish_job(
        self,
        *,
        job_id: str,
        owner: str,
        lease_generation: int,
        state: str,
        result: dict[str, Any] | None,
        error: str | None,
        now: str,
    ) -> dict[str, Any]: ...

    def expired_jobs(
        self, *, now: str, limit: int | None = None
    ) -> list[dict[str, Any]]: ...


def completed_stages(checkpoint: dict[str, Any] | None) -> list[str]:
    """Extract list of completed stages from a checkpoint, tolerating empty/missing."""
    if not checkpoint:
        return []
    completed = checkpoint.get("completed")
    if completed is None:
        return []
    return list(completed)


def checkpoint_data(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    """Extract custom data dictionary from a checkpoint, tolerating empty/missing."""
    if not checkpoint:
        return {}
    data = checkpoint.get("data")
    if data is None:
        return {}
    return dict(data)


def default_idempotency_key(experiment_id: str, *, dry_run: bool) -> str:
    """Generate the default idempotency key format for experiment runs."""
    return f"lab.run:{experiment_id}:{int(dry_run)}"


def _shift(now: str, seconds: int) -> str:
    """Parse an ISO timestamp, add offset seconds, and re-emit standard format."""
    cleaned = now
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    shifted = dt + timedelta(seconds=seconds)
    if shifted.tzinfo is not None:
        shifted = shifted.astimezone(UTC)
    return shifted.strftime("%Y-%m-%dT%H:%M:%SZ")


def enqueue(
    store: JobStore,
    experiment_id: str,
    *,
    idempotency_key: str | None = None,
    dry_run: bool = False,
    now: str | None = None,
) -> tuple[LabJob, bool]:
    """Enqueue a job, maintaining strict single-instance idempotency."""
    if now is None:
        now = utc_now_iso()
    if idempotency_key is None:
        idempotency_key = default_idempotency_key(experiment_id, dry_run=dry_run)
    job_id = f"labjob_{uuid4().hex[:12]}"
    row, created = store.enqueue_job(
        job_id=job_id,
        idempotency_key=idempotency_key,
        experiment_id=experiment_id,
        dry_run=dry_run,
        now=now,
    )
    return LabJob.from_row(row), created


def claim(
    store: JobStore,
    *,
    owner: str,
    now: str | None = None,
    lease_seconds: int = LEASE_SECONDS,
) -> LabJob | None:
    """Attempt to claim one queued or expired job and take an exclusive lease."""
    if now is None:
        now = utc_now_iso()
    lease_expires_at = _shift(now, lease_seconds)
    row = store.claim_job(owner=owner, now=now, lease_expires_at=lease_expires_at)
    if row is None:
        return None
    return LabJob.from_row(row)


def heartbeat(
    store: JobStore,
    job: LabJob,
    *,
    owner: str,
    now: str | None = None,
    lease_seconds: int = LEASE_SECONDS,
) -> LabJob:
    """Extend the lease on a running job, ensuring exclusive execution.

    It calls store.heartbeat_job and returns the refreshed LabJob.
    LabJobLeaseLost propagates if the lease was lost.
    """
    if now is None:
        now = utc_now_iso()
    lease_expires_at = _shift(now, lease_seconds)
    row = store.heartbeat_job(
        job_id=job.job_id,
        owner=owner,
        lease_generation=job.lease_generation,
        lease_expires_at=lease_expires_at,
        now=now,
    )
    return LabJob.from_row(row)


def reconcile(store: JobStore, *, now: str | None = None) -> list[LeaseExpiry]:
    """Scan and report currently expired jobs that are adoptable (no side-effects)."""
    if now is None:
        now = utc_now_iso()
    rows = store.expired_jobs(now=now)
    results = []
    for row in rows:
        results.append(
            LeaseExpiry(
                job_id=row["job_id"],
                lease_owner=row.get("lease_owner"),
                expired_at=row.get("lease_expires_at"),
                attempt=row["attempt"],
            )
        )
    return results


def request_cancel(
    store: JobStore, job_id: str, *, now: str | None = None
) -> LabJob | None:
    """Request a cancel, moving to terminal immediately if queued, or setting flag."""
    if now is None:
        now = utc_now_iso()
    row = store.request_job_cancel(job_id=job_id, now=now)
    if row is None:
        return None
    return LabJob.from_row(row)


def execute(
    store: JobStore,
    job: LabJob,
    stages: Sequence[JobStage],
    *,
    owner: str,
    now_fn: Callable[[], str] = utc_now_iso,
    lease_seconds: int = LEASE_SECONDS,
) -> LabJob:
    """Execute stages sequentially, fencing with a lease and writing checkpoints."""
    checkpoint = dict(job.checkpoint)
    completed = list(completed_stages(checkpoint))
    data = dict(checkpoint_data(checkpoint))
    current_generation = job.lease_generation

    try:
        for stage in stages:
            now = now_fn()
            re_read_row = store.get_job(job.job_id)
            if re_read_row is None:
                raise ValueError(f"Job {job.job_id} vanished from database during execution")

            re_read_job = LabJob.from_row(re_read_row)
            if re_read_job.cancel_requested:
                finished_row = store.finish_job(
                    job_id=job.job_id,
                    owner=owner,
                    lease_generation=current_generation,
                    state=CANCELLED,
                    result={"stages": completed, "data": data},
                    error=f"cancelled before stage {stage.name}",
                    now=now,
                )
                return LabJob.from_row(finished_row)

            if stage.name in completed:
                continue

            try:
                res = stage.run(job, data)
                if res is not None:
                    data.update(res)
                completed.append(stage.name)
            except JobCancelled as exc:
                now = now_fn()
                finished_row = store.finish_job(
                    job_id=job.job_id,
                    owner=owner,
                    lease_generation=current_generation,
                    state=CANCELLED,
                    result={"stages": completed, "data": data},
                    error=str(exc),
                    now=now,
                )
                return LabJob.from_row(finished_row)

            now = now_fn()
            new_lease_expires_at = _shift(now, lease_seconds)
            new_checkpoint = {"completed": completed, "data": data}
            store.checkpoint_job(
                job_id=job.job_id,
                owner=owner,
                lease_generation=current_generation,
                checkpoint=new_checkpoint,
                lease_expires_at=new_lease_expires_at,
                now=now,
            )

        now = now_fn()
        finished_row = store.finish_job(
            job_id=job.job_id,
            owner=owner,
            lease_generation=current_generation,
            state=SUCCEEDED,
            result={"stages": completed, "data": data},
            error=None,
            now=now,
        )
        return LabJob.from_row(finished_row)

    except LabJobLeaseLost:
        raise
    except Exception as exc:
        now = now_fn()
        try:
            finished_row = store.finish_job(
                job_id=job.job_id,
                owner=owner,
                lease_generation=current_generation,
                state=FAILED,
                result={"stages": completed, "data": data},
                error=f"{type(exc).__name__}: {exc}",
                now=now,
            )
            return LabJob.from_row(finished_row)
        except LabJobLeaseLost:
            raise
        except Exception:
            raise exc from None


def run_once(
    store: JobStore,
    stages: Sequence[JobStage],
    *,
    owner: str,
    now_fn: Callable[[], str] = utc_now_iso,
    lease_seconds: int = LEASE_SECONDS,
) -> LabJob | None:
    """Atomically claim one available job and run its stages under owner lease."""
    now = now_fn()
    job = claim(store, owner=owner, now=now, lease_seconds=lease_seconds)
    if job is None:
        return None
    return execute(
        store,
        job,
        stages,
        owner=owner,
        now_fn=now_fn,
        lease_seconds=lease_seconds,
    )


def campaign_stages(store: Any, evaluator: Any) -> tuple[JobStage, ...]:
    """Build the production single-stage campaign pipeline."""
    def run_campaign_stage(job: LabJob, data: dict[str, Any]) -> dict[str, Any]:
        from omniagentos.lab.campaign import run_experiment

        disposition = run_experiment(
            store, evaluator, job.experiment_id, dry_run=job.dry_run
        )
        return {"disposition": disposition.value}

    return (JobStage(name="campaign", run=run_campaign_stage),)


__all__ = [
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "NON_TERMINAL_JOB_STATES",
    "TERMINAL_JOB_STATES",
    "LEASE_SECONDS",
    "LabJob",
    "JobCancelled",
    "JobStage",
    "LeaseExpiry",
    "JobStore",
    "completed_stages",
    "checkpoint_data",
    "default_idempotency_key",
    "enqueue",
    "claim",
    "heartbeat",
    "reconcile",
    "request_cancel",
    "execute",
    "run_once",
    "ensure_lab_jobs_routine",
    "campaign_stages",
]


def ensure_lab_jobs_routine(store: Any) -> dict[str, Any]:
    """Register the drain tick once, idempotently.

    Without this the /run route enqueues durably and nothing ever advances the
    queue — the 'built but wired to nothing' failure this codebase keeps
    hitting. Mirrors ensure_improve_dispatcher_routine.
    """
    from omniagentos.scheduler.routines import (
        LAB_JOBS_ROUTINE_NAME,
        lab_jobs_drain_routine,
    )
    from omniagentos.scheduler.store import RoutinesStore

    r_store = RoutinesStore(store)
    for existing in r_store.list_routines():
        if existing.get("name") == LAB_JOBS_ROUTINE_NAME:
            return existing
    return r_store.create_routine(lab_jobs_drain_routine())
