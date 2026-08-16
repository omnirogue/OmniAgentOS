"""Fleet-scale-200: planner task cap, per-run slot ceiling, and the scheduler's
admission math at 20 concurrent swarms.

The work package raised three numbers that only deliver parallelism TOGETHER:

* ``planner.MAX_TASKS``            how wide a plan may be (20 -> 30);
* ``planner.TARGET_N_HARD_CEILING`` how wide a plan may ASK to run (10 -> 20);
* ``scheduler.MAX_SLOTS``          how wide a coordinator will actually run it.

A mismatch is silent: the planner writes target_concurrency=20 and the
coordinator clamps it back to 10 with no error anywhere. These tests pin the
three together and then simulate the real thing -- a 30-task plan, 20 concurrent
swarms, 220 swarm session slots -- and assert the scheduler admits more than the
old ceiling of 10.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.swarm import planner as planner_module
from omniagentos.swarm.contracts import SwarmTaskSpec
from omniagentos.swarm.planner import (
    MAX_TASKS,
    SOLO_MAX_TASKS,
    SOLO_RATIO_THRESHOLD,
    TARGET_N_HARD_CEILING,
    TARGET_N_MAX,
    TARGET_N_MIN,
    SwarmPlanError,
    build_plan,
    parallelism_stats,
)
from omniagentos.swarm.scheduler import MAX_SLOTS, _RunState

from .scheduler_fakes import FakeLimits, make_harness, make_scheduler

TARGET_CAP_ENV = "OMNIAGENTOS_SWARM_TARGET_CAP"


def _raw_task(task_id: str, deps: tuple[str, ...] = (), agent: int = 10) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": task_id.upper(),
        "description": f"do {task_id}",
        "depends_on": list(deps),
        "owned_paths": [f"src/{task_id}.txt"],
        "est_agent_minutes": agent,
        "est_manual_minutes": 30,
        "acceptance": f"{task_id} done",
        "verify_command": f"pytest tests/{task_id}",
    }


class TestCeilingsMoveTogether:
    def test_planner_and_scheduler_ceilings_are_equal(self) -> None:
        """The planner's env-override ceiling and the coordinator's per-run clamp
        MUST be the same number, or one of them is dead config."""
        assert TARGET_N_HARD_CEILING == MAX_SLOTS == 20

    def test_optimizer_can_recommend_the_full_width(self) -> None:
        from omniagentos.swarm.optimize import _MAX_CONCURRENCY_CEILING

        assert _MAX_CONCURRENCY_CEILING == MAX_SLOTS

    def test_solo_rules_are_unchanged(self) -> None:
        """Raising the ceilings must not change WHEN a swarm happens at all."""
        assert SOLO_MAX_TASKS == 2
        assert SOLO_RATIO_THRESHOLD == 1.5
        assert TARGET_N_MIN == 2
        assert TARGET_N_MAX == 5  # default cap without the env override


class TestTargetCap:
    def test_default_is_the_unchanged_five(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(TARGET_CAP_ENV, raising=False)
        assert planner_module._target_cap() == TARGET_N_MAX

    @pytest.mark.parametrize("value", [5, 8, 12, 19, 20])
    def test_env_override_is_honored_up_to_the_new_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, value: int
    ) -> None:
        monkeypatch.setenv(TARGET_CAP_ENV, str(value))
        assert planner_module._target_cap() == value

    @pytest.mark.parametrize("value", [21, 50, 1000])
    def test_env_override_clamps_above_the_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, value: int
    ) -> None:
        monkeypatch.setenv(TARGET_CAP_ENV, str(value))
        assert planner_module._target_cap() == TARGET_N_HARD_CEILING

    @pytest.mark.parametrize("value", ["0", "1", "-5"])
    def test_env_override_never_drops_below_the_floor(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(TARGET_CAP_ENV, value)
        assert planner_module._target_cap() == TARGET_N_MIN

    @pytest.mark.parametrize("value", ["", "  ", "twenty", "20.5"])
    def test_unparseable_override_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(TARGET_CAP_ENV, value)
        assert planner_module._target_cap() == TARGET_N_MAX

    def test_parallelism_stats_can_now_reach_twenty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TARGET_CAP_ENV, "20")
        # 30 independent 10-minute tasks: ratio 30, critical path 10.
        specs = [SwarmTaskSpec(id=f"t{i}", title=f"T{i}", est_agent_minutes=10) for i in range(30)]
        ratio, target_n = parallelism_stats(specs)
        assert ratio == pytest.approx(30.0)
        assert target_n == 20

    def test_parallelism_stats_still_defaults_to_five(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(TARGET_CAP_ENV, raising=False)
        specs = [SwarmTaskSpec(id=f"t{i}", title=f"T{i}", est_agent_minutes=10) for i in range(30)]
        _, target_n = parallelism_stats(specs)
        assert target_n == TARGET_N_MAX


class TestTaskCap:
    def test_max_tasks_is_thirty(self) -> None:
        assert MAX_TASKS == 30

    def test_thirty_tasks_are_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TARGET_CAP_ENV, "20")
        plan = build_plan("goal", [_raw_task(f"t{i}") for i in range(MAX_TASKS)])
        worker_ids = [task.id for task in plan.tasks if task.id != "integration"]
        assert len(worker_ids) == 30
        assert plan.mode == "swarm"
        assert plan.target_n >= TARGET_N_MIN

    def test_thirty_one_tasks_are_rejected(self) -> None:
        with pytest.raises(SwarmPlanError, match="task cap exceeded: 31 > 30"):
            build_plan("goal", [_raw_task(f"t{i}") for i in range(MAX_TASKS + 1)])

    def test_plan_time_target_n_is_capped_by_the_ALLOCATOR_not_the_slot_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DOCUMENTED FINDING (fleet-scale-200), asserted so it cannot regress
        into a surprise.

        A 30-task plan has parallelism_ratio 30, so the ratio rule alone would
        ask for the full ``_target_cap()``. It does not: ``build_plan`` runs the
        capacity allocator, whose ``DEFAULT_REPO_WRITER_SLOTS = 4`` policy
        ("four concurrent writers per repository") caps ``alloc.worker_count``
        and therefore the plan's opening ``target_n``.

        That policy is deliberately NOT touched by this package -- it is a
        collision-safety decision, not a fleet-capacity one. The consequence to
        understand: the 200-agent target is met ACROSS repos (20 swarms x N),
        not by 20 writers inside one repo. Plan-time ``target_n`` is only an
        opening bid; the coordinator's resize
        (``_recompute_target`` = min(run_cap, demand, fair_share, MAX_SLOTS))
        is what sets live width, which is what TestSchedulerAdmissionAtScale
        exercises.
        """
        from omniagentos.allocation.capacity import DEFAULT_REPO_WRITER_SLOTS

        monkeypatch.setenv(TARGET_CAP_ENV, "20")
        plan = build_plan("goal", [_raw_task(f"t{i}") for i in range(MAX_TASKS)])
        assert plan.parallelism_ratio == pytest.approx(30.0)
        assert plan.target_n <= DEFAULT_REPO_WRITER_SLOTS
        assert any("fanout_cap" in note for note in plan.assumptions)

    def test_provisioning_default_no_longer_hardcodes_ten(self) -> None:
        """``provision_run(max_concurrency=...)`` is the run row's upper bound and
        the intake dispatch path never passes it. A default of 10 there would
        have clamped every production run back to the OLD ceiling regardless of
        MAX_SLOTS."""
        import inspect

        from omniagentos.swarm.planner import provision_run

        default = inspect.signature(provision_run).parameters["max_concurrency"].default
        assert default == MAX_SLOTS == TARGET_N_HARD_CEILING

    def test_the_prompt_advertises_the_new_cap(self) -> None:
        """The planner LLM is told the allowed range in its prompt. A hardcoded
        number there would cap real plans at 20 no matter what validation
        accepts, so the rule must interpolate MAX_TASKS."""
        source = Path(planner_module.__file__).read_text(encoding="utf-8")
        assert 'f"- 2-{MAX_TASKS} tasks' in source


class TestSchedulerAdmissionAtScale:
    """The scheduler's own math, against a real 30-task provisioned run."""

    @staticmethod
    def _wide_harness(tmp_path: Path, *, capacity: int, tasks: int = 30) -> Any:
        specs = [{"id": f"t{i}", "est": 10} for i in range(tasks)]
        harness = make_harness(
            tmp_path, specs, integration=True, target_n=2, max_concurrency=MAX_SLOTS
        )
        harness.limits = FakeLimits(capacity=capacity, world=harness.world)
        return harness

    @staticmethod
    def _target_for(harness: Any) -> int:
        scheduler = make_scheduler(harness, limits=harness.limits)
        # Isolate the ADMISSION MATH: growing target_n normally starts worker
        # threads, which would immediately claim tasks and race the assertion.
        scheduler._ensure_workers = lambda state: None  # type: ignore[method-assign]
        state = _RunState(run_id=harness.run_id, working_dir=str(harness.workdir))
        run = harness.dal.get_run(harness.run_id)
        scheduler._recompute_target(state, run)
        with state.cond:
            return state.target_n

    def test_thirty_task_run_opens_more_than_ten_slots(self, tmp_path: Path) -> None:
        """The headline: with capacity available, a 30-task run runs 20 wide --
        the old MAX_SLOTS=10 would have clamped this to 10."""
        harness = self._wide_harness(tmp_path, capacity=220)
        try:
            assert self._target_for(harness) == MAX_SLOTS
            assert self._target_for(harness) > 10
        finally:
            harness.close()

    def test_twenty_concurrent_swarms_each_still_exceed_the_old_ceiling(
        self, tmp_path: Path
    ) -> None:
        """200 agents across 20 swarms: fair_share = 220 // 20 = 11 > 10.

        This is the whole point of the package -- at the OLD ceilings
        (120 global / 20 reserved => 100 swarm slots) twenty swarms would have
        got 100 // 20 = 5 slots each.
        """
        harness = self._wide_harness(tmp_path, capacity=220)
        try:
            # 19 more active runs so active_run_count() == 20.
            conn = sqlite3.connect(harness.db_path)
            now = utc_now_iso()
            try:
                for index in range(19):
                    conn.execute(
                        "INSERT INTO swarm_runs (id, status, created_at, updated_at) "
                        "VALUES (?, 'running', ?, ?)",
                        (f"swr_filler{index}", now, now),
                    )
                conn.commit()
            finally:
                conn.close()
            assert harness.dal.active_run_count() == 20
            target = self._target_for(harness)
            assert target == 11, "fair share of 220 slots across 20 swarms"
            assert target > 10

            # Same fleet at the OLD ceiling would have been 5.
            assert max(1, 100 // 20) == 5
        finally:
            harness.close()

    def test_ledger_still_binds_when_capacity_is_scarce(self, tmp_path: Path) -> None:
        """A raised ceiling must not make the run ignore the live ledger."""
        harness = self._wide_harness(tmp_path, capacity=6)
        try:
            assert self._target_for(harness) == 6
        finally:
            harness.close()

    def test_run_cap_clamps_a_hostile_max_concurrency(self, tmp_path: Path) -> None:
        """``swarm_runs.max_concurrency`` is caller-supplied; MAX_SLOTS is the
        backstop that keeps one run from claiming the entire fleet."""
        harness = self._wide_harness(tmp_path, capacity=220)
        try:
            scheduler = make_scheduler(harness, limits=harness.limits)
            assert scheduler._run_cap({"max_concurrency": 500}) == MAX_SLOTS
            assert scheduler._run_cap({"max_concurrency": 7}) == 7
            assert scheduler._run_cap({"max_concurrency": 0}) == MAX_SLOTS
            assert scheduler._run_cap({"max_concurrency": -3}) == 1
        finally:
            harness.close()
