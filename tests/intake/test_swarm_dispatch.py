"""WP10: ``execute="swarm"``/``"auto"`` — the Swarm Mode intake wiring.

Swarm is an EXECUTE MODE, never a lane value: its branch runs BEFORE
``_resolve_dispatch_lane`` (which raises on any non-fast/longhaul/auto lane).
A single solo plan falls through to the EXISTING orchestrate path with zero
behavior change; bundles dispatch independently (solo → orchestrate card,
swarm-worthy → provisioned swarm run); fleet admission queues over-cap runs;
activation stays behind ``OMNIAGENTOS_SWARM_EXECUTE`` (off = provision-only).
Explicit lanes keep absolute priority and are never intercepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import omniagentos.api.main  # noqa: F401 -- break the package's documented import cycle.
import omniagentos.intake.service as intake_service
import omniagentos.swarm.scheduler as swarm_scheduler
from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.intake.contracts import RefinedSpec
from omniagentos.intake.service import dispatch_spec
from omniagentos.policy import load_policy
from omniagentos.swarm.contracts import SwarmPlan, SwarmTaskSpec
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.plan_safety import PlanSafetyError


@dataclass
class _FakeResult:
    run_id: str = "orch_test0001"
    status: str = "done"
    tasks: list[Any] = field(default_factory=lambda: [object()])
    spec_note_path: str | None = None
    escalations: list[Any] = field(default_factory=list)


@dataclass
class _StubOrchestrateRunner:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, goal: str, **kwargs: Any) -> Any:
        self.calls.append({"goal": goal, **kwargs})
        return _FakeResult(run_id=kwargs.get("run_id") or "orch_test0001")


@dataclass
class _RecordingPlanner:
    plans: list[SwarmPlan]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, brief_or_spec: Any, working_dir: str, **kwargs: Any) -> list[SwarmPlan]:
        self.calls.append({"spec": brief_or_spec, "working_dir": working_dir, **kwargs})
        return list(self.plans)


class _FakeSchedulerRecorder:
    def __init__(self, handle: Any = None) -> None:
        self.started: list[str] = []
        self._handle = handle if handle is not None else object()

    def start_run(self, run_id: str, *, block: bool = False) -> Any:
        self.started.append(run_id)
        return self._handle


def _task(task_id: str, **overrides: Any) -> SwarmTaskSpec:
    base: dict[str, Any] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "description": f"Do {task_id}",
        "est_manual_minutes": 30,
        "est_agent_minutes": 10,
        "owned_paths": [f"src/{task_id}"],
        "acceptance": f"{task_id} works",
    }
    base.update(overrides)
    return SwarmTaskSpec(**base)


def _swarm_plan(goal: str = "Build the parallel thing", n: int = 3) -> SwarmPlan:
    return SwarmPlan(
        goal=goal,
        tasks=[_task(f"task-{i}") for i in range(1, n + 1)],
        mode="swarm",
        parallelism_ratio=2.5,
        target_n=3,
    )


def _solo_plan(goal: str = "One small bounded change") -> SwarmPlan:
    return SwarmPlan(goal=goal, tasks=[_task("task-1")], mode="solo", target_n=1)


def _stores(tmp_path: Path) -> tuple[Any, CollabStore, Any, str]:
    db_path = str(tmp_path / "swarm-intake.db")
    collab = CollabStore(db_path)
    return collab._store, collab, load_policy(), db_path


def _spec(**overrides: Any) -> RefinedSpec:
    base: dict[str, Any] = {
        "title": "Ship the parallel greeter",
        "description": "Refresh three unrelated docs sections quickly.",
        "acceptance_criteria": ["all sections updated"],
    }
    base.update(overrides)
    return RefinedSpec(**base)


@pytest.fixture(autouse=True)
def _flag_off_and_scoped_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OMNIAGENTOS_SWARM_EXECUTE", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))


def test_swarm_branch_bypasses_lane_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """execute="swarm" never reaches _resolve_dispatch_lane (swarm is not a lane)."""
    store, collab, cfg, db_path = _stores(tmp_path)

    def _boom(*_args: Any, **_kwargs: Any) -> str | None:
        raise AssertionError("_resolve_dispatch_lane must not run for execute='swarm'")

    monkeypatch.setattr(intake_service, "_resolve_dispatch_lane", _boom)
    planner = _RecordingPlanner([_swarm_plan()])

    result = dispatch_spec(store, collab, cfg, _spec(), execute="swarm", swarm_planner=planner)

    assert len(planner.calls) == 1
    assert result["execute"] == "swarm"
    assert result["swarm_run_id"].startswith("swr_")
    # Root card is returned like the other dispatch shapes and lives on the board.
    root_card = collab.get_board_task(result["board_task"]["id"])
    assert root_card is not None
    assert root_card["swarm_run_id"] == result["swarm_run_id"]
    # lane stays NULL — swarm membership is swarm_run_id only (043 CHECK frozen).
    assert root_card.get("lane") is None


def test_flag_off_is_provision_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OMNIAGENTOS_SWARM_EXECUTE unset → provision-only: no scheduler start ever."""
    store, collab, cfg, db_path = _stores(tmp_path)

    def _no_scheduler() -> Any:
        raise AssertionError("scheduler must not be constructed with the flag off")

    monkeypatch.setattr(swarm_scheduler, "_default_scheduler", _no_scheduler)
    planner = _RecordingPlanner([_swarm_plan()])

    result = dispatch_spec(store, collab, cfg, _spec(), execute="swarm", swarm_planner=planner)

    assert result["swarm"]["activated"] is False
    dal = SwarmDal(db_path)
    try:
        run = dal.get_run(result["swarm_run_id"])
        assert run is not None
        assert run["status"] == "planning"  # provisioned, never started
        # Child cards exist for every planned task (plus the root card).
        members = dal.tasks_for_run(result["swarm_run_id"])
        assert len(members) == 3 + 1
    finally:
        dal.close()


def test_flag_on_activates_provisioned_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, collab, cfg, _ = _stores(tmp_path)
    fake = _FakeSchedulerRecorder()
    monkeypatch.setenv("OMNIAGENTOS_SWARM_EXECUTE", "1")
    monkeypatch.setattr(swarm_scheduler, "_default_scheduler", lambda: fake)
    planner = _RecordingPlanner([_swarm_plan()])

    result = dispatch_spec(store, collab, cfg, _spec(), execute="swarm", swarm_planner=planner)

    assert fake.started == [result["swarm_run_id"]]
    assert result["swarm"]["activated"] is True


def test_solo_plan_falls_through_to_orchestrate_unchanged(tmp_path: Path) -> None:
    """A single solo plan → the EXISTING orchestrate path (zero behavior change)."""
    store, collab, cfg, db_path = _stores(tmp_path)
    planner = _RecordingPlanner([_solo_plan()])
    runner = _StubOrchestrateRunner()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        execute="auto",
        orchestrate_runner=runner,
        swarm_planner=planner,
    )

    # The existing path ran: one orchestration with the composed spec as goal.
    assert len(runner.calls) == 1
    assert "Ship the parallel greeter" in runner.calls[0]["goal"]
    assert result["execute"] == "orchestrate"
    assert result["run_id"].startswith("orch_")
    assert "swarm_run_id" not in result
    # And NO swarm run was provisioned.
    dal = SwarmDal(db_path)
    try:
        assert dal.list_runs() == []
    finally:
        dal.close()


def test_bundles_dispatch_as_independent_cards(tmp_path: Path) -> None:
    """N unrelated asks refuse atomically until grouped provisioning exists.

    The legacy test name stays stable for the fast-lane selector. P1-SAFETY
    deliberately replaced independent side effects with a fail-closed refusal;
    P5 group creation must land before multi-bundle dispatch can be re-enabled.
    """
    store, collab, cfg, db_path = _stores(tmp_path)
    solo = _solo_plan("Rename the launch doc header")
    swarm = _swarm_plan("Parallel refactor of three modules")
    planner = _RecordingPlanner([solo, swarm])
    runner = _StubOrchestrateRunner()

    dal = SwarmDal(db_path)
    try:
        with pytest.raises(PlanSafetyError) as excinfo:
            dispatch_spec(
                store,
                collab,
                cfg,
                _spec(),
                execute="swarm",
                orchestrate_runner=runner,
                swarm_planner=planner,
            )
        assert any(issue.code == "multiple_bundles" for issue in excinfo.value.decision.issues)
        assert runner.calls == []
        assert dal.list_runs() == []
        assert collab.list_board_tasks() == []
    finally:
        dal.close()


def test_fleet_admission_queues_over_cap_and_never_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run #cap+1 provisions as queued (no activation), intake returns normally."""
    store, collab, cfg, db_path = _stores(tmp_path)
    monkeypatch.setattr(
        "omniagentos.routing.limit_state.load_swarm_config",
        lambda *a, **k: {"max_concurrent_swarms": 1},
    )
    fake = _FakeSchedulerRecorder()
    monkeypatch.setenv("OMNIAGENTOS_SWARM_EXECUTE", "1")
    monkeypatch.setattr(swarm_scheduler, "_default_scheduler", lambda: fake)

    seed = SwarmDal(db_path)
    try:
        run_id = str(seed.create_run(working_dir="/tmp/ws", goal="occupies the fleet")["id"])
        seed.set_run_status(run_id, "running")
    finally:
        seed.close()

    planner = _RecordingPlanner([_swarm_plan()])
    result = dispatch_spec(store, collab, cfg, _spec(), execute="swarm", swarm_planner=planner)

    assert result["swarm"]["queued"] is True
    assert result["swarm"]["status"] == "queued"
    assert result["swarm"]["activated"] is False
    assert fake.started == []  # a queued run must never start a coordinator


def test_explicit_lane_is_never_intercepted(tmp_path: Path) -> None:
    """lane="fast" + execute="swarm" → the existing path; the planner never runs."""
    store, collab, cfg, db_path = _stores(tmp_path)
    planner = _RecordingPlanner([_swarm_plan()])
    runner = _StubOrchestrateRunner()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        execute="swarm",
        lane="fast",
        orchestrate_runner=runner,
        swarm_planner=planner,
    )

    assert planner.calls == []
    assert result["execute"] == "orchestrate"
    assert len(runner.calls) == 1
    dal = SwarmDal(db_path)
    try:
        assert dal.list_runs() == []
    finally:
        dal.close()


def test_speed_priority_pins_land_in_plan_json_and_card_swarm_json(
    tmp_path: Path,
) -> None:
    """D10: priority/pins/speed reach _provision_swarm_bundle and are recorded
    ADDITIVELY — plan_json["params"] on the run, "speed" on every card's
    swarm_json (root included) for the router's tier floor."""
    import json as _json

    store, collab, cfg, db_path = _stores(tmp_path)
    planner = _RecordingPlanner([_swarm_plan()])

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        execute="swarm",
        priority="quality",
        pins={"executor_model_or_lineage": "opus"},
        speed="ultra",
        swarm_planner=planner,
    )

    dal = SwarmDal(db_path)
    try:
        run = dal.get_run(result["swarm_run_id"])
        assert run is not None
        plan_json = _json.loads(run["plan_json"])
        assert plan_json["params"] == {
            "priority": "quality",
            "pins": {"executor_model_or_lineage": "opus"},
            "speed": "ultra",
        }
        members = dal.tasks_for_run(result["swarm_run_id"])
        assert len(members) == 3 + 1
        for member in members:
            swarm_json = _json.loads(member["swarm_json"] or "{}")
            assert swarm_json["speed"] == "ultra"
            # The fingerprint chain is untouched: plan_hash still stamped.
            assert swarm_json["plan_hash"] == plan_json["plan_hash"]
    finally:
        dal.close()


def test_runs_without_speed_keep_current_behavior(tmp_path: Path) -> None:
    """Additive contract: no speed/priority/pins → plan_json has NO params key
    and cards carry no speed stamp (old rows / old callers unchanged)."""
    import json as _json

    store, collab, cfg, db_path = _stores(tmp_path)
    planner = _RecordingPlanner([_swarm_plan()])

    result = dispatch_spec(store, collab, cfg, _spec(), execute="swarm", swarm_planner=planner)

    dal = SwarmDal(db_path)
    try:
        run = dal.get_run(result["swarm_run_id"])
        assert run is not None
        plan_json = _json.loads(run["plan_json"])
        assert "params" not in plan_json
        for member in dal.tasks_for_run(result["swarm_run_id"]):
            swarm_json = _json.loads(member["swarm_json"] or "{}")
            assert "speed" not in swarm_json
    finally:
        dal.close()


def test_quick_placeholder_card_is_archived_when_superseded(tmp_path: Path) -> None:
    store, collab, cfg, _ = _stores(tmp_path)
    placeholder = BoardTask(title="Planning the work…", status=BoardTaskStatus.OPEN)
    collab.create_board_task(placeholder)
    planner = _RecordingPlanner([_swarm_plan()])

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        execute="swarm",
        board_task_id=placeholder.id,
        swarm_planner=planner,
    )

    superseded = collab.get_board_task(placeholder.id)
    assert superseded is not None
    assert superseded["archived_at"] is not None
    # The returned card is the run's ROOT card, not the placeholder.
    assert result["board_task"]["id"] != placeholder.id
