"""Intake planner provision-boundary safety (P1-SAFETY).

``provision_plan`` must refuse degraded / empty plans before any project row
or dispatch side effect. Mutation ``refusal-creates-run`` moves creation ahead
of the gate; these tests then fail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.contracts import HarnessType
from omniagentos.intake.planner import (
    PlannedTask,
    PlanningState,
    ProjectPlan,
    ProvisionResult,
    RouteDecision,
    plan_goal,
    provision_plan,
)
from omniagentos.policy import load_policy
from omniagentos.swarm.plan_safety import PlanSafetyError


class _StubHierarchyDAL:
    """Mirror ``tests/intake/test_planner.py`` hierarchy stub + call recording."""

    def __init__(self, store: Any, existing: list[dict[str, Any]] | None = None) -> None:
        from omniagentos.projects import ProjectStore

        self._projects = ProjectStore(store) if store is not None else None
        self.projects: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []
        self._seq = 0
        for seed in existing or []:
            pid = str(seed["id"])
            if self._projects is not None:
                pid = str(self._projects.create_project({"id": pid, "name": seed["name"]})["id"])
            self.projects.append({"id": pid, "name": seed["name"], "parent_project_id": None})

    def create_project(
        self, name: str, *, parent_project_id: str | None = None, description: str = ""
    ) -> str:
        self.create_calls.append(
            {
                "name": name,
                "parent_project_id": parent_project_id,
                "description": description,
            }
        )
        self._seq += 1
        if self._projects is not None:
            pid = str(self._projects.create_project({"name": name})["id"])
        else:
            pid = f"proj_{self._seq}"
        self.projects.append({"id": pid, "name": name, "parent_project_id": parent_project_id})
        return pid

    def list_projects(self) -> list[dict[str, Any]]:
        return list(self.projects)


def _stores(tmp_path: Path):
    collab = CollabStore(str(tmp_path / "plan-safety.db"))
    return collab._store, collab, load_policy()


def test_provision_refuses_degraded_model_unavailable_with_zero_side_effects(
    tmp_path: Path,
) -> None:
    store, collab, policy = _stores(tmp_path)
    hierarchy = _StubHierarchyDAL(store)
    plan = plan_goal("ship the weekly report", llm=lambda *a, **k: None)
    assert plan.planning_state.status == "degraded"
    assert plan.planning_state.reason == "model_unavailable"
    route = RouteDecision(decision="new", project_name=plan.project_name)

    with pytest.raises(PlanSafetyError) as excinfo:
        provision_plan(
            store, collab, policy, plan, route, hierarchy, harness=HarnessType.MOCK.value
        )

    assert excinfo.value.decision.is_ready is False
    assert hierarchy.create_calls == []
    assert hierarchy.projects == []
    assert collab.list_board_tasks() == []


def test_provision_refuses_empty_model_plan_degraded(tmp_path: Path) -> None:
    store, collab, policy = _stores(tmp_path)
    hierarchy = _StubHierarchyDAL(store)
    plan = ProjectPlan(
        project_name="Empty",
        tasks=[{"title": "placeholder"}],  # type: ignore[list-item]
        planning_state=PlanningState(
            status="degraded", source="heuristic", reason="empty_model_plan"
        ),
    )
    route = RouteDecision(decision="new", project_name="Empty")

    with pytest.raises(PlanSafetyError):
        provision_plan(
            store, collab, policy, plan, route, hierarchy, harness=HarnessType.MOCK.value
        )
    assert hierarchy.create_calls == []


def test_provision_refuses_empty_task_list_without_creating_project(tmp_path: Path) -> None:
    store, collab, policy = _stores(tmp_path)
    hierarchy = _StubHierarchyDAL(store)
    plan = ProjectPlan(project_name="NoTasks", tasks=[], sub_projects=[])
    route = RouteDecision(decision="new", project_name="NoTasks")

    with pytest.raises(PlanSafetyError) as excinfo:
        provision_plan(
            store, collab, policy, plan, route, hierarchy, harness=HarnessType.MOCK.value
        )
    assert any(i.code == "empty_plan" for i in excinfo.value.decision.issues)
    assert hierarchy.create_calls == []


@pytest.mark.parametrize(
    "owned_path",
    [".", "configs/policy.yaml", "../outside"],
    ids=["root-wide", "protected", "escaping"],
)
def test_provision_refuses_unsafe_owned_paths_before_hierarchy_side_effects(
    tmp_path: Path,
    owned_path: str,
) -> None:
    store, collab, policy = _stores(tmp_path)
    hierarchy = _StubHierarchyDAL(store)
    plan = ProjectPlan(
        project_name="Unsafe scope",
        tasks=[PlannedTask(title="Mutate files", owned_paths=[owned_path])],
    )

    with pytest.raises(PlanSafetyError):
        provision_plan(
            store,
            collab,
            policy,
            plan,
            RouteDecision(decision="new"),
            hierarchy,
            harness=HarnessType.MOCK.value,
        )

    assert hierarchy.create_calls == []
    assert collab.list_board_tasks() == []


def test_provision_ready_plan_still_creates_project(tmp_path: Path) -> None:
    store, collab, policy = _stores(tmp_path)
    hierarchy = _StubHierarchyDAL(store)
    plan = ProjectPlan(
        project_name="Reports",
        tasks=[{"title": "Weekly report", "acceptance_criteria": ["emailed"]}],  # type: ignore[list-item]
        planning_state=PlanningState(status="ready", source="model", reason=None),
    )
    route = RouteDecision(decision="new", project_name="Reports")
    result = provision_plan(
        store, collab, policy, plan, route, hierarchy, harness=HarnessType.MOCK.value
    )
    assert isinstance(result, ProvisionResult)
    assert hierarchy.create_calls
    assert result.task_count == 1


def test_refusal_gate_precedes_create_project_in_source() -> None:
    """Mutation audit target for ``refusal-creates-run`` on intake.planner."""
    source = Path("omniagentos/intake/planner.py").read_text(encoding="utf-8")
    fn = source.index("def provision_plan(")
    next_def = source.find("\ndef ", fn + 1)
    chunk = source[fn : next_def if next_def != -1 else fn + 8000]
    live_gate = None
    for i, line in enumerate(chunk.splitlines()):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if 'plan.planning_state.status == "degraded"' in stripped:
            live_gate = i
            break
    assert live_gate is not None, "provision_plan must refuse degraded plans"
    create_line = None
    for i, line in enumerate(chunk.splitlines()):
        if "hierarchy.create_project(" in line and not line.lstrip().startswith("#"):
            create_line = i
            break
    assert create_line is not None
    assert live_gate < create_line
    assert "PlanSafetyError" in "\n".join(chunk.splitlines()[live_gate:create_line])
