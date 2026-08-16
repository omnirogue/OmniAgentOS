"""Intake service swarm-dispatch safety (P1-SAFETY).

Covers multi-bundle interim refusal (persist none), provision-time revalidation,
and activation recheck. Named mutations: ``multi-bundle-keeps-first``,
``refusal-creates-run``, ``activation-skips-recheck``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import omniagentos.api.main  # noqa: F401 -- break the package import cycle.
import omniagentos.intake.service as intake_service
from omniagentos.collab.store import CollabStore
from omniagentos.intake.contracts import RefinedSpec
from omniagentos.intake.service import dispatch_spec
from omniagentos.policy import load_policy
from omniagentos.swarm.contracts import SwarmPlan, SwarmTaskSpec
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.plan_safety import PlanSafetyError


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


def _unsafe_dot_plan() -> SwarmPlan:
    return SwarmPlan(
        goal="own everything",
        tasks=[_task("t1", owned_paths=["."])],
        mode="swarm",
        target_n=1,
    )


def _fallback_shape_plan() -> SwarmPlan:
    return SwarmPlan(
        goal="degraded",
        tasks=[_task("task-1", owned_paths=[])],
        assumptions=[
            "planner degraded to flat solo plan: unparseable",
            "planner failed closed: unparseable",
        ],
        mode="solo",
        target_n=1,
    )


@dataclass
class _RecordingPlanner:
    plans: list[SwarmPlan]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, brief_or_spec: Any, working_dir: str, **kwargs: Any) -> list[SwarmPlan]:
        self.calls.append({"spec": brief_or_spec, "working_dir": working_dir, **kwargs})
        return list(self.plans)


def _stores(tmp_path: Path) -> tuple[Any, CollabStore, Any, str]:
    db_path = str(tmp_path / "swarm-intake-safety.db")
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


def test_multi_bundle_raises_and_persists_no_runs(tmp_path: Path) -> None:
    store, collab, policy, db_path = _stores(tmp_path)
    dal = SwarmDal(db_path)
    planner = _RecordingPlanner(
        [
            _swarm_plan("fix the API", n=3),
            _swarm_plan("refresh the docs site", n=3),
        ]
    )
    try:
        with pytest.raises(PlanSafetyError) as excinfo:
            dispatch_spec(
                store,
                collab,
                policy,
                _spec(),
                execute="swarm",
                swarm_planner=planner,
            )
        assert any(i.code == "multiple_bundles" for i in excinfo.value.decision.issues)
        assert dal.list_runs() == []
        assert collab.list_board_tasks() == []
    finally:
        dal.close()


def test_dot_scope_plan_refused_with_zero_side_effects(tmp_path: Path) -> None:
    store, collab, policy, db_path = _stores(tmp_path)
    dal = SwarmDal(db_path)
    planner = _RecordingPlanner([_unsafe_dot_plan()])
    try:
        with pytest.raises(PlanSafetyError) as excinfo:
            dispatch_spec(
                store,
                collab,
                policy,
                _spec(),
                execute="swarm",
                swarm_planner=planner,
            )
        codes = {i.code for i in excinfo.value.decision.issues}
        assert "root_wide_ownership" in codes
        assert dal.list_runs() == []
    finally:
        dal.close()


def test_bundle_helper_rejects_before_project_resolution_or_run_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, collab, _policy, db_path = _stores(tmp_path)
    dal = SwarmDal(db_path)
    resolver_called = False

    def _unexpected_resolve(*args: Any, **kwargs: Any) -> tuple[str, str]:
        nonlocal resolver_called
        del args, kwargs
        resolver_called = True
        return "", ""

    monkeypatch.setattr(
        intake_service,
        "_resolve_or_create_orchestration_project",
        _unexpected_resolve,
    )
    try:
        with pytest.raises(PlanSafetyError):
            intake_service._provision_swarm_bundle(
                store,
                collab,
                dal,
                _unsafe_dot_plan(),
                _spec(),
                project_id=None,
                budget_usd_max=None,
            )
        assert resolver_called is False
        assert dal.list_runs() == []
        assert collab.list_board_tasks() == []
    finally:
        dal.close()


def test_fallback_planner_shape_refused_before_provision(tmp_path: Path) -> None:
    store, collab, policy, db_path = _stores(tmp_path)
    dal = SwarmDal(db_path)
    planner = _RecordingPlanner([_fallback_shape_plan()])
    try:
        with pytest.raises(PlanSafetyError) as excinfo:
            dispatch_spec(
                store,
                collab,
                policy,
                _spec(),
                execute="swarm",
                swarm_planner=planner,
            )
        assert excinfo.value.decision.is_ready is False
        assert dal.list_runs() == []
    finally:
        dal.close()


def test_safe_swarm_plan_provisions_run(tmp_path: Path) -> None:
    store, collab, policy, db_path = _stores(tmp_path)
    dal = SwarmDal(db_path)
    planner = _RecordingPlanner([_swarm_plan()])
    try:
        result = dispatch_spec(
            store,
            collab,
            policy,
            _spec(),
            execute="swarm",
            swarm_planner=planner,
        )
        assert result.get("swarm_run_id")
        assert dal.list_runs()
    finally:
        dal.close()


def test_provision_bundle_gate_precedes_dal_provision_in_source() -> None:
    source = Path("omniagentos/intake/service.py").read_text(encoding="utf-8")
    marker = "def _provision_swarm_bundle("
    start = source.index(marker)
    next_def = source.find("\ndef ", start + 1)
    chunk = source[start : next_def if next_def != -1 else start + 8000]
    live_gate = None
    for i, line in enumerate(chunk.splitlines()):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "assert_plan_safe_for_provision(" in stripped:
            live_gate = i
            break
    assert live_gate is not None
    provision_line = None
    for i, line in enumerate(chunk.splitlines()):
        if "_swarm_provision_run(" in line and not line.lstrip().startswith("#"):
            provision_line = i
            break
    assert provision_line is not None
    assert live_gate < provision_line


def test_activation_recheck_present_in_source() -> None:
    """Mutation ``activation-skips-recheck``: live assert immediately before activate."""
    source = Path("omniagentos/intake/service.py").read_text(encoding="utf-8")
    act_idx = source.index("activate_run_if_enabled(run_id)")
    # Walk backward to the nearest non-comment assert_plan_safe call.
    window = source[max(0, act_idx - 500) : act_idx]
    live = False
    for line in window.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "assert_plan_safe_for_provision(plan" in stripped:
            live = True
    assert live, "activation path must revalidate with a live assert_plan_safe_for_provision"
