"""Deterministic synthetic swarm runs for objective-function properties."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from omniagentos.contracts import new_id
from omniagentos.swarm.contracts import SWARM_EVENT_KIND, SwarmPlan, SwarmTaskSpec
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.planner import parallelism_stats, provision_run

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class SynthTask:
    key: str
    est_manual_minutes: int
    est_agent_minutes: int
    status: str = "done"
    attempt_end_reasons: tuple[str, ...] = ("completed",)
    actual_minutes: float = 0.0
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class SynthRun:
    tasks: tuple[SynthTask, ...]
    wall_seconds: float
    target_concurrency: int
    stall_seconds: float = 0.0
    status: str = "completed"

    @property
    def work_axes(self) -> tuple[int, int, float, int]:
        """Independent evidence that a pair is ordered by delivered work."""
        completed = tuple(task for task in self.tasks if task.status == "done")
        return (
            len(completed),
            sum(bool(task.attempt_end_reasons) for task in completed),
            sum(task.actual_minutes for task in completed),
            sum(task.est_manual_minutes for task in completed),
        )


def completed_run(
    task_count: int,
    *,
    wall_minutes: float,
    target_concurrency: int,
) -> SynthRun:
    return SynthRun(
        tasks=tuple(
            SynthTask(
                key=f"task-{index}",
                est_manual_minutes=60,
                est_agent_minutes=10,
                actual_minutes=10.0,
            )
            for index in range(task_count)
        ),
        wall_seconds=wall_minutes * 60.0,
        target_concurrency=target_concurrency,
    )


def zero_work_run(*, wall_minutes: float = 1.0) -> SynthRun:
    return SynthRun(
        tasks=(
            SynthTask(
                key="planned-but-never-started",
                est_manual_minutes=60,
                est_agent_minutes=10,
                status="open",
                attempt_end_reasons=(),
                actual_minutes=0.0,
            ),
        ),
        wall_seconds=wall_minutes * 60.0,
        target_concurrency=1,
    )


def _iso(instant: datetime) -> str:
    return instant.isoformat().replace("+00:00", "Z")


def synth_run(dal: SwarmDal, spec: SynthRun, *, started_at: datetime = T0) -> str:
    task_specs = [
        SwarmTaskSpec(
            id=task.key,
            title=task.key,
            depends_on=list(task.depends_on),
            complexity="simple",
            est_manual_minutes=task.est_manual_minutes,
            est_agent_minutes=task.est_agent_minutes,
            owned_paths=[f"src/{task.key}.txt"],
            acceptance="delivered",
        )
        for task in spec.tasks
    ]
    parallelism_ratio, _ = parallelism_stats(task_specs)
    plan = SwarmPlan(
        goal="objective property fixture",
        tasks=task_specs,
        mode="swarm",
        version=1,
        target_n=spec.target_concurrency,
        parallelism_ratio=parallelism_ratio,
    )
    provisioned = provision_run(
        plan,
        dal=dal,
        working_dir=".",
        max_concurrency=10,
        write_plan_doc=False,
    )
    run_id = str(provisioned["run"]["id"])
    card_ids = provisioned["card_ids"]

    for task in spec.tasks:
        reasons = task.attempt_end_reasons
        per_attempt_seconds = (
            task.actual_minutes * 60.0 / len(reasons) if reasons else 0.0
        )
        cursor = started_at
        for seq, reason in enumerate(reasons):
            ended_at = cursor + timedelta(seconds=per_attempt_seconds)
            dal._connection.execute(
                "INSERT INTO swarm_attempts "
                "(id, swarm_run_id, board_task_id, seq, session_id, provider, model, tier, "
                "account_id, started_at, ended_at, end_reason, detail) "
                "VALUES (?, ?, ?, ?, NULL, 'test', 'test', 'standard', NULL, ?, ?, ?, '')",
                (
                    new_id("swa"),
                    run_id,
                    card_ids[task.key],
                    seq,
                    _iso(cursor),
                    _iso(ended_at),
                    reason,
                ),
            )
            cursor = ended_at
        dal._connection.execute(
            "UPDATE board_tasks SET status = ? WHERE id = ?",
            (task.status, card_ids[task.key]),
        )

    finished_at = started_at + timedelta(seconds=spec.wall_seconds)
    dal._connection.execute(
        "UPDATE swarm_runs SET started_at = ?, finished_at = ?, status = ?, "
        "target_concurrency = ? WHERE id = ?",
        (
            _iso(started_at),
            _iso(finished_at),
            spec.status,
            spec.target_concurrency,
            run_id,
        ),
    )
    if spec.stall_seconds:
        dal._connection.execute(
            "INSERT INTO events "
            "(ts, type, actor, action, target_type, target_id, payload_json, trace_id) "
            "VALUES (?, ?, 'test', 'rate_limit_stall', 'swarm_run', ?, ?, '')",
            (
                _iso(started_at),
                SWARM_EVENT_KIND,
                run_id,
                json.dumps({"seconds": spec.stall_seconds}),
            ),
        )
    return run_id


def independent_tasks(count: int, *, actual_minutes: float = 30.0) -> Sequence[SynthTask]:
    return tuple(
        SynthTask(
            key=f"parallel-{index}",
            est_manual_minutes=30,
            est_agent_minutes=30,
            actual_minutes=actual_minutes,
        )
        for index in range(count)
    )
