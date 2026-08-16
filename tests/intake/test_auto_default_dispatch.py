"""Swarm auto-default: the orchestrator chooses solo-vs-swarm ITSELF by default.

A plain ``execute="orchestrate"`` dispatch with NO explicit lane — the machine
default behind the quick front door's planned lane — upgrades to the WP10
``auto`` plan-first decision, but ONLY when ``OMNIAGENTOS_SWARM_EXECUTE`` is on
AND the scheduler's git-checkout requirement can be met: a project root with a
``.git``, or NO project at all (the cockpit composer) — the swarm provision
path git-initializes the managed workspace it creates. Every other combination
keeps today's path byte-identically: the swarm planner never runs. Explicit
lanes and explicit non-orchestrate execute values keep absolute priority. A
pre-created orchestrations row superseded by swarm run(s) is terminal-closed so
the resume sweep never conducts a phantom.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

import omniagentos.api.main  # noqa: F401 -- break the package's documented import cycle.
import omniagentos.intake.service as intake_service
import omniagentos.swarm.scheduler as swarm_scheduler
from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import HarnessType, new_id
from omniagentos.intake.contracts import RefinedSpec
from omniagentos.intake.orchestrations import OrchestrationsDal
from omniagentos.intake.service import dispatch_spec
from omniagentos.policy import load_policy
from omniagentos.projects import ProjectStore
from omniagentos.swarm.contracts import SwarmPlan, SwarmTaskSpec
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.planner import SwarmHeadroom


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
    def __init__(self) -> None:
        self.started: list[str] = []

    def start_run(self, run_id: str, *, block: bool = False) -> Any:
        self.started.append(run_id)
        return object()


def _task(task_id: str) -> SwarmTaskSpec:
    return SwarmTaskSpec(
        id=task_id,
        title=f"Task {task_id}",
        description=f"Do {task_id}",
        est_manual_minutes=30,
        est_agent_minutes=10,
        owned_paths=[f"src/{task_id}"],
        acceptance=f"{task_id} works",
    )


def _swarm_plan() -> SwarmPlan:
    return SwarmPlan(
        goal="Build the parallel thing",
        tasks=[_task(f"task-{i}") for i in range(1, 4)],
        mode="swarm",
        parallelism_ratio=2.5,
        target_n=3,
    )


def _solo_plan() -> SwarmPlan:
    return SwarmPlan(
        goal="One small bounded change", tasks=[_task("task-1")], mode="solo", target_n=1
    )


def _stores(tmp_path: Path) -> tuple[Any, CollabStore, Any, str]:
    db_path = str(tmp_path / "auto-default.db")
    collab = CollabStore(db_path)
    return collab._store, collab, load_policy(), db_path


def _spec() -> RefinedSpec:
    return RefinedSpec(
        title="Ship the parallel greeter",
        description="Refresh three unrelated docs sections quickly.",
        acceptance_criteria=["all sections updated"],
    )


def _project(store: Any, tmp_path: Path, *, git: bool = True) -> str:
    """A project whose root dir is (optionally) a git checkout."""
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True) if git else root.mkdir(parents=True)
    project = ProjectStore(cast(Any, store)).create_project(
        {"name": "Repo under test", "root_dirs": [str(root)]}
    )
    return str(project["id"])


@pytest.fixture(autouse=True)
def _flag_off_and_scoped_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OMNIAGENTOS_SWARM_EXECUTE", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))


def _flag_on(monkeypatch: pytest.MonkeyPatch, fake: _FakeSchedulerRecorder) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SWARM_EXECUTE", "1")
    monkeypatch.setattr(swarm_scheduler, "_default_scheduler", lambda: fake)


def test_default_dispatch_routes_through_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag on + git working dir: a default orchestrate dispatch plans first and
    provisions the swarm the planner chose — the orchestrator decided itself."""
    store, collab, cfg, db_path = _stores(tmp_path)
    project_id = _project(store, tmp_path, git=True)
    fake = _FakeSchedulerRecorder()
    _flag_on(monkeypatch, fake)
    planner = _RecordingPlanner([_swarm_plan()])

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        project_id=project_id,
        execute="orchestrate",
        swarm_planner=planner,
    )

    assert len(planner.calls) == 1
    # The rate-limit context rode along into the planner's solo-vs-swarm rule.
    assert isinstance(planner.calls[0]["headroom"], SwarmHeadroom)
    assert result["execute"] == "swarm"
    assert result["swarm_run_id"].startswith("swr_")
    assert fake.started == [result["swarm_run_id"]]
    assert result["swarm"]["activated"] is True


def test_default_dispatch_solo_plan_falls_through_to_orchestrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upgrade only ADDS the decision: a solo plan runs the existing
    orchestrate path exactly as if the upgrade had never happened."""
    store, collab, cfg, db_path = _stores(tmp_path)
    project_id = _project(store, tmp_path, git=True)
    _flag_on(monkeypatch, _FakeSchedulerRecorder())
    planner = _RecordingPlanner([_solo_plan()])
    runner = _StubOrchestrateRunner()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        project_id=project_id,
        execute="orchestrate",
        orchestrate_runner=runner,
        swarm_planner=planner,
    )

    assert len(planner.calls) == 1
    assert len(runner.calls) == 1
    assert result["execute"] == "orchestrate"
    assert result["run_id"].startswith("orch_")
    dal = SwarmDal(db_path)
    try:
        assert dal.list_runs() == []
    finally:
        dal.close()


def test_flag_off_keeps_the_legacy_path_untouched(tmp_path: Path) -> None:
    """OMNIAGENTOS_SWARM_EXECUTE unset (autouse fixture): the swarm planner never
    runs and the dispatch is the plain orchestrate path, byte-identically."""
    store, collab, cfg, db_path = _stores(tmp_path)
    project_id = _project(store, tmp_path, git=True)
    planner = _RecordingPlanner([_swarm_plan()])
    runner = _StubOrchestrateRunner()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        project_id=project_id,
        execute="orchestrate",
        orchestrate_runner=runner,
        swarm_planner=planner,
    )

    assert planner.calls == []
    assert len(runner.calls) == 1
    assert result["execute"] == "orchestrate"
    dal = SwarmDal(db_path)
    try:
        assert dal.list_runs() == []
    finally:
        dal.close()


def test_non_git_working_dir_keeps_the_legacy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag on but the project root is not a git checkout (scheduler requirement
    unmet): no upgrade, no planning."""
    store, collab, cfg, _ = _stores(tmp_path)
    project_id = _project(store, tmp_path, git=False)
    _flag_on(monkeypatch, _FakeSchedulerRecorder())
    planner = _RecordingPlanner([_swarm_plan()])
    runner = _StubOrchestrateRunner()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        project_id=project_id,
        execute="orchestrate",
        orchestrate_runner=runner,
        swarm_planner=planner,
    )

    assert planner.calls == []
    assert result["execute"] == "orchestrate"


def test_projectless_dispatch_upgrades_and_git_inits_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag on + NO project (the cockpit composer): the router still decides.
    The swarm provision path creates its own managed workspace and
    git-initializes it WITH a root commit — the scheduler refuses non-git
    working dirs and its snapshot machinery needs a resolvable HEAD."""
    store, collab, cfg, db_path = _stores(tmp_path)
    fake = _FakeSchedulerRecorder()
    _flag_on(monkeypatch, fake)
    planner = _RecordingPlanner([_swarm_plan()])

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        execute="orchestrate",
        swarm_planner=planner,
    )

    assert len(planner.calls) == 1
    assert result["execute"] == "swarm"
    assert fake.started == [result["swarm_run_id"]]
    dal = SwarmDal(db_path)
    try:
        runs = dal.list_runs()
    finally:
        dal.close()
    assert len(runs) == 1
    workspace = Path(str(runs[0]["working_dir"]))
    assert (workspace / ".git").exists()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    assert head.returncode == 0, head.stderr


def test_projectless_solo_plan_falls_through_to_orchestrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Projectless + solo verdict: the existing orchestrate path runs exactly
    as if the upgrade had never happened — no swarm run rows."""
    store, collab, cfg, db_path = _stores(tmp_path)
    _flag_on(monkeypatch, _FakeSchedulerRecorder())
    planner = _RecordingPlanner([_solo_plan()])
    runner = _StubOrchestrateRunner()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        execute="orchestrate",
        orchestrate_runner=runner,
        swarm_planner=planner,
    )

    assert len(planner.calls) == 1
    assert len(runner.calls) == 1
    assert result["execute"] == "orchestrate"
    dal = SwarmDal(db_path)
    try:
        assert dal.list_runs() == []
    finally:
        dal.close()


def _high_headroom() -> SwarmHeadroom:
    return SwarmHeadroom(
        level="high",
        available_for_swarm=20,
        mean_provider_pressure=0.1,
        low_headroom_slots=5,
        solo_ratio_threshold=1.5,
    )


def test_speed_fast_raises_the_solo_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fastest-dial topology bias: with HIGH headroom (standard 1.5 rule) a
    speed='fast' dispatch hands the planner the RAISED low-headroom threshold —
    swarm coordination must pay off harder on the fastest lane."""
    import omniagentos.swarm.planner as swarm_planner_module

    store, collab, cfg, _ = _stores(tmp_path)
    project_id = _project(store, tmp_path, git=True)
    _flag_on(monkeypatch, _FakeSchedulerRecorder())
    monkeypatch.setattr(swarm_planner_module, "swarm_headroom", lambda **_kw: _high_headroom())
    planner = _RecordingPlanner([_swarm_plan()])

    dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        project_id=project_id,
        execute="orchestrate",
        swarm_planner=planner,
        speed="fast",
    )

    assert planner.calls[0]["headroom"].solo_ratio_threshold == 2.5


def test_default_speed_keeps_the_standard_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No dial speed: the HIGH-headroom standard rule reaches the planner
    untouched — the bias is fast-only."""
    import omniagentos.swarm.planner as swarm_planner_module

    store, collab, cfg, _ = _stores(tmp_path)
    project_id = _project(store, tmp_path, git=True)
    _flag_on(monkeypatch, _FakeSchedulerRecorder())
    monkeypatch.setattr(swarm_planner_module, "swarm_headroom", lambda **_kw: _high_headroom())
    planner = _RecordingPlanner([_swarm_plan()])

    dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        project_id=project_id,
        execute="orchestrate",
        swarm_planner=planner,
    )

    assert planner.calls[0]["headroom"].solo_ratio_threshold == 1.5


def test_execute_single_hard_suppresses_the_auto_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D10: execute='single' (the Mode dial's Single Task) never reaches swarm
    planning — even with OMNIAGENTOS_SWARM_EXECUTE on AND a git working dir
    (every auto-default guard met). The dispatch orchestrates, exactly as the
    upgrade-free path always has."""
    store, collab, cfg, db_path = _stores(tmp_path)
    project_id = _project(store, tmp_path, git=True)
    _flag_on(monkeypatch, _FakeSchedulerRecorder())
    planner = _RecordingPlanner([_swarm_plan()])
    runner = _StubOrchestrateRunner()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        project_id=project_id,
        execute="single",
        orchestrate_runner=runner,
        swarm_planner=planner,
    )

    assert planner.calls == []
    assert len(runner.calls) == 1
    assert result["execute"] == "orchestrate"
    assert result["run_id"].startswith("orch_")
    dal = SwarmDal(db_path)
    try:
        assert dal.list_runs() == []
    finally:
        dal.close()


def test_omitted_execute_keeps_the_auto_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API backcompat (D10): a caller that names NO execute value keeps today's
    auto-default behavior — the upgrade still applies when the guards are met."""
    store, collab, cfg, _ = _stores(tmp_path)
    project_id = _project(store, tmp_path, git=True)
    fake = _FakeSchedulerRecorder()
    _flag_on(monkeypatch, fake)
    planner = _RecordingPlanner([_swarm_plan()])

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        project_id=project_id,
        execute="orchestrate",
        swarm_planner=planner,
    )

    assert len(planner.calls) == 1
    assert result["execute"] == "swarm"


def test_explicit_lane_keeps_absolute_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lane="fast" is never intercepted, even with every auto-default guard met."""
    store, collab, cfg, _ = _stores(tmp_path)
    project_id = _project(store, tmp_path, git=True)
    _flag_on(monkeypatch, _FakeSchedulerRecorder())
    planner = _RecordingPlanner([_swarm_plan()])
    runner = _StubOrchestrateRunner()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        project_id=project_id,
        execute="orchestrate",
        lane="fast",
        orchestrate_runner=runner,
        swarm_planner=planner,
    )

    assert planner.calls == []
    assert len(runner.calls) == 1
    assert result["execute"] == "orchestrate"


def test_explicit_non_orchestrate_execute_is_never_upgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """execute="readonly" (an explicit posture) never reaches swarm planning."""
    store, collab, cfg, _ = _stores(tmp_path)
    project_id = _project(store, tmp_path, git=True)
    _flag_on(monkeypatch, _FakeSchedulerRecorder())

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("_dispatch_swarm must not run for execute='readonly'")

    monkeypatch.setattr(intake_service, "_dispatch_swarm", _boom)

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        harness=HarnessType.MOCK.value,
        project_id=project_id,
        execute="readonly",
    )

    assert result["execute"] == "readonly"
    assert result["run_id"] is not None


def test_superseded_orchestration_row_is_terminal_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The quick front door pre-creates a queued orchestrations row; when the
    auto decision routes to swarm instead, that row must not linger for the
    resume sweep to conduct as a phantom duplicate."""
    store, collab, cfg, db_path = _stores(tmp_path)
    project_id = _project(store, tmp_path, git=True)
    _flag_on(monkeypatch, _FakeSchedulerRecorder())
    planner = _RecordingPlanner([_swarm_plan()])

    placeholder = BoardTask(title="Planning the work…", status=BoardTaskStatus.OPEN)
    collab.create_board_task(placeholder)
    run_id = new_id("orch")
    lifecycle = OrchestrationsDal(db_path)
    try:
        lifecycle.create(run_id, board_task_id=placeholder.id, working_dir="")
    finally:
        lifecycle.close()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        project_id=project_id,
        execute="orchestrate",
        board_task_id=placeholder.id,
        orchestration_run_id=run_id,
        swarm_planner=planner,
    )

    assert result["execute"] == "swarm"
    lifecycle = OrchestrationsDal(db_path)
    try:
        row = lifecycle.get(run_id)
    finally:
        lifecycle.close()
    assert row is not None
    assert row["status"] == "cancelled"
    assert result["swarm_run_id"] in str(row["error"])
    # The instant placeholder card was superseded by the run's root card.
    superseded = collab.get_board_task(placeholder.id)
    assert superseded is not None
    assert superseded["archived_at"] is not None
