"""Unit tests for the PLAN step: planner, router, effort scaling, provisioning.

All Fable calls are injected seams — no CLI is touched. The hierarchy DAL is stubbed
(W3-hierarchy owns the concrete implementation), and dispatch uses the real
CollabStore + control-plane store so provisioning is exercised end-to-end."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omniagentos.collab.store import CollabStore
from omniagentos.contracts import HarnessType
from omniagentos.intake.planner import (
    ProjectPlan,
    RouteDecision,
    effort_for,
    estimate_complexity,
    plan_and_provision,
    plan_goal,
    provision_plan,
    route_project,
)
from omniagentos.policy import load_policy

# ---------------------------------------------------------------------------
# Fable seam fakes
# ---------------------------------------------------------------------------


def _planner_llm(reply: dict[str, Any] | None, *, capture: list[str] | None = None):
    def _llm(_prompt: str, _schema: dict[str, Any], effort: str) -> dict[str, Any] | None:
        if capture is not None:
            capture.append(effort)
        return reply

    return _llm


def _router_llm(reply: dict[str, Any] | None):
    def _llm(_prompt: str, _schema: dict[str, Any]) -> dict[str, Any] | None:
        return reply

    return _llm


class _StubHierarchyDAL:
    """Stub for W3-hierarchy's DAL.

    Persists a REAL project row (so a dispatched task's project_id FK is satisfied)
    but records the parent link itself, standing in for migration 031's
    ``projects.parent_project_id`` which isn't in this worktree. ``existing`` are
    pre-seeded projects the router may nest under."""

    def __init__(self, store: Any = None, existing: list[dict[str, Any]] | None = None) -> None:
        from omniagentos.projects import ProjectStore

        self._projects = ProjectStore(store) if store is not None else None
        self.projects: list[dict[str, Any]] = []
        self._seq = 0
        for seed in existing or []:
            pid = str(seed["id"])
            if self._projects is not None:
                pid = str(self._projects.create_project({"id": pid, "name": seed["name"]})["id"])
            self.projects.append({"id": pid, "name": seed["name"], "parent_project_id": None})

    def create_project(
        self, name: str, *, parent_project_id: str | None = None, description: str = ""
    ) -> str:
        self._seq += 1
        if self._projects is not None:
            pid = str(self._projects.create_project({"name": name})["id"])
        else:
            pid = f"proj_{self._seq}"
        self.projects.append({"id": pid, "name": name, "parent_project_id": parent_project_id})
        return pid

    def list_projects(self) -> list[dict[str, Any]]:
        return list(self.projects)


# ---------------------------------------------------------------------------
# PLAN
# ---------------------------------------------------------------------------


def test_plan_goal_produces_a_flat_project() -> None:
    reply = {
        "project_name": "Health Endpoint",
        "description": "Expose a healthcheck.",
        "complexity": "simple",
        "tasks": [
            {"title": "Add /health", "acceptance_criteria": ["returns 200"]},
        ],
    }
    plan = plan_goal("add a health endpoint", llm=_planner_llm(reply))
    assert plan.project_name == "Health Endpoint"
    assert plan.is_decomposed() is False
    assert len(plan.tasks) == 1
    assert plan.tasks[0].title == "Add /health"
    assert plan.planning_state.model_dump() == {
        "status": "ready",
        "source": "model",
        "reason": None,
    }


def test_complex_goal_decomposes_into_sub_projects_and_tasks() -> None:
    reply = {
        "project_name": "Support Platform",
        "complexity": "complex",
        "sub_projects": [
            {
                "name": "Ingestion",
                "tasks": [
                    {"title": "Build poller", "acceptance_criteria": ["polls"]},
                    {"title": "Normalize events"},
                ],
            },
            {
                "name": "Dashboard",
                "tasks": [{"title": "Board UI"}],
            },
        ],
    }
    plan = plan_goal(
        "build an end-to-end support platform with ingestion and a dashboard",
        llm=_planner_llm(reply),
    )
    assert plan.is_decomposed() is True
    assert [sp.name for sp in plan.sub_projects] == ["Ingestion", "Dashboard"]
    assert len(plan.sub_projects[0].tasks) == 2
    assert len(plan.sub_projects[1].tasks) == 1


def test_plan_falls_back_to_flat_plan_when_fable_unavailable() -> None:
    plan = plan_goal("ship the weekly report", llm=_planner_llm(None))
    assert plan.project_name
    assert len(plan.tasks) == 1
    assert plan.tasks[0].acceptance_criteria
    assert plan.planning_state.model_dump() == {
        "status": "degraded",
        "source": "heuristic",
        "reason": "model_unavailable",
    }


def test_empty_decomposition_degrades_to_a_task() -> None:
    # A decomposition with no tasks anywhere is useless -> heuristic flat plan.
    reply = {"project_name": "Empty", "sub_projects": [{"name": "Nothing", "tasks": []}]}
    plan = plan_goal("do a thing", llm=_planner_llm(reply))
    assert plan.tasks  # something dispatchable exists
    assert plan.planning_state.model_dump() == {
        "status": "degraded",
        "source": "heuristic",
        "reason": "empty_model_plan",
    }


# ---------------------------------------------------------------------------
# Effort scaling
# ---------------------------------------------------------------------------


def test_estimate_complexity_bands() -> None:
    assert estimate_complexity("rename a button") == "simple"
    assert (
        estimate_complexity(
            "build an end-to-end analytics platform integrating multiple pipelines "
            "with a dashboard and alerting"
        )
        == "complex"
    )


def test_effort_scales_with_complexity() -> None:
    assert effort_for("simple") == "high"
    assert effort_for("standard") == "high"
    assert effort_for("complex") == "max"

    # And the planner actually RUNS at that effort: capture what the seam received.
    simple_effort: list[str] = []
    plan_goal("rename a label", llm=_planner_llm(None, capture=simple_effort))
    assert simple_effort == ["high"]

    complex_effort: list[str] = []
    plan_goal(
        "design a platform with multiple microservices, a data pipeline, and a "
        "dashboard integrating several systems end-to-end",
        llm=_planner_llm(None, capture=complex_effort),
    )
    assert complex_effort == ["max"]


def test_explicit_effort_override_wins() -> None:
    captured: list[str] = []
    plan_goal("anything", llm=_planner_llm(None, capture=captured), effort="low")
    assert captured == ["low"]


# ---------------------------------------------------------------------------
# ROUTER
# ---------------------------------------------------------------------------


def test_router_new_when_no_existing_projects() -> None:
    plan = ProjectPlan(project_name="Fresh Thing")
    decision = route_project(plan, [], llm=_router_llm({"decision": "existing"}))
    # No existing projects: nothing to nest under, so always new (model not consulted).
    assert decision.decision == "new"
    assert decision.project_name == "Fresh Thing"


def test_router_picks_existing_when_fable_says_so() -> None:
    plan = ProjectPlan(project_name="Add SSO")
    existing = [{"id": "proj_auth", "name": "Auth"}, {"id": "proj_ui", "name": "UI"}]
    decision = route_project(
        plan,
        existing,
        llm=_router_llm({"decision": "existing", "parent_project_id": "proj_auth"}),
    )
    assert decision.decision == "existing"
    assert decision.parent_project_id == "proj_auth"


def test_router_ignores_existing_verdict_with_unknown_parent() -> None:
    plan = ProjectPlan(project_name="Add SSO")
    existing = [{"id": "proj_auth", "name": "Auth"}]
    decision = route_project(
        plan,
        existing,
        llm=_router_llm({"decision": "existing", "parent_project_id": "proj_ghost"}),
    )
    # Parent isn't real -> not actionable -> new.
    assert decision.decision == "new"


def test_router_picks_new_when_fable_says_new() -> None:
    plan = ProjectPlan(project_name="Standalone")
    existing = [{"id": "proj_x", "name": "X"}]
    decision = route_project(plan, existing, llm=_router_llm({"decision": "new"}))
    assert decision.decision == "new"


# ---------------------------------------------------------------------------
# PROVISIONING (stubbed hierarchy DAL, real dispatch)
# ---------------------------------------------------------------------------


def _stores(tmp_path: Path):
    collab = CollabStore(str(tmp_path / "plan.db"))
    return collab._store, collab, load_policy()


def test_provision_flat_plan_creates_project_and_dispatches(tmp_path: Path) -> None:
    store, collab, policy = _stores(tmp_path)
    hierarchy = _StubHierarchyDAL(store)
    plan = ProjectPlan(
        project_name="Reports",
        tasks=[
            {"title": "Weekly report", "acceptance_criteria": ["emailed"]},  # type: ignore[list-item]
            {"title": "Monthly report"},  # type: ignore[list-item]
        ],
    )
    route = RouteDecision(decision="new", project_name="Reports")

    result = provision_plan(
        store, collab, policy, plan, route, hierarchy, harness=HarnessType.MOCK.value
    )
    assert len(hierarchy.projects) == 1
    root = hierarchy.projects[0]
    assert root["parent_project_id"] is None
    assert result.task_count == 2
    # Every dispatched task landed as a live board card linked to a run.
    board = collab.list_board_tasks()
    assert len(board) == 2
    assert all(row["run_id"] for row in board)


def test_provision_decomposed_plan_nests_sub_projects(tmp_path: Path) -> None:
    store, collab, policy = _stores(tmp_path)
    hierarchy = _StubHierarchyDAL(store)
    plan = ProjectPlan(
        project_name="Platform",
        sub_projects=[
            {  # type: ignore[list-item]
                "name": "Ingestion",
                "tasks": [{"title": "Poller"}, {"title": "Normalizer"}],
            },
            {"name": "Dashboard", "tasks": [{"title": "Board UI"}]},  # type: ignore[list-item]
        ],
    )
    route = RouteDecision(decision="new", project_name="Platform")

    result = provision_plan(
        store, collab, policy, plan, route, hierarchy, harness=HarnessType.MOCK.value
    )
    # Root + two sub-projects, both nested under the root.
    assert len(hierarchy.projects) == 3
    root_id = result.root_project_id
    subs = [p for p in hierarchy.projects if p["parent_project_id"] == root_id]
    assert len(subs) == 2
    assert result.task_count == 3


def test_provision_existing_route_nests_root_under_parent(tmp_path: Path) -> None:
    store, collab, policy = _stores(tmp_path)
    hierarchy = _StubHierarchyDAL(store, existing=[{"id": "proj_parent", "name": "Parent"}])
    plan = ProjectPlan(project_name="Feature", tasks=[{"title": "Do it"}])  # type: ignore[list-item]
    route = RouteDecision(decision="existing", parent_project_id="proj_parent")

    result = provision_plan(
        store, collab, policy, plan, route, hierarchy, harness=HarnessType.MOCK.value
    )
    root = next(p for p in hierarchy.projects if p["id"] == result.root_project_id)
    assert root["parent_project_id"] == "proj_parent"


def test_provision_plan_threads_dial_directives_into_every_dispatch(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """D10 (F1): execute/speed/priority/pins ride VERBATIM into every per-task
    dispatch_spec call — root tasks and sub-project tasks alike."""
    import omniagentos.intake.service as service

    store, collab, policy = _stores(tmp_path)
    hierarchy = _StubHierarchyDAL(store)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        service,
        "dispatch_spec",
        lambda *_a, **kw: calls.append(kw) or {"board_task": {"id": "bt"}},
    )
    plan = ProjectPlan(
        project_name="Dial",
        tasks=[{"title": "Root task"}],  # type: ignore[list-item]
        sub_projects=[
            {"name": "Sub", "tasks": [{"title": "Sub task"}]},  # type: ignore[list-item]
        ],
    )
    route = RouteDecision(decision="new", project_name="Dial")

    pins = {"planner_model": "gemini-3.6-flash"}
    provision_plan(
        store,
        collab,
        policy,
        plan,
        route,
        hierarchy,
        execute="single",
        speed="fast",
        priority="fast",
        pins=pins,
    )

    assert len(calls) == 2
    for call in calls:
        assert call["execute"] == "single"
        assert call["speed"] == "fast"
        assert call["priority"] == "fast"
        assert call["pins"] == pins


def test_provision_plan_defaults_keep_legacy_dispatch(tmp_path: Path, monkeypatch: Any) -> None:
    """F3 regression: callers that name no dial dispatch exactly as today."""
    import omniagentos.intake.service as service

    store, collab, policy = _stores(tmp_path)
    hierarchy = _StubHierarchyDAL(store)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        service,
        "dispatch_spec",
        lambda *_a, **kw: calls.append(kw) or {"board_task": {"id": "bt"}},
    )
    plan = ProjectPlan(project_name="Legacy", tasks=[{"title": "Do it"}])  # type: ignore[list-item]

    provision_plan(store, collab, policy, plan, RouteDecision(decision="new"), hierarchy)

    assert calls[0]["execute"] == "readonly"
    assert calls[0]["speed"] is None
    assert calls[0]["priority"] is None
    assert calls[0]["pins"] is None


def test_plan_and_provision_end_to_end(tmp_path: Path) -> None:
    store, collab, policy = _stores(tmp_path)
    hierarchy = _StubHierarchyDAL(store, existing=[{"id": "proj_auth", "name": "Auth"}])
    plan_reply = {
        "project_name": "SSO",
        "complexity": "standard",
        "tasks": [{"title": "OIDC login", "acceptance_criteria": ["works"]}],
    }
    route_reply = {"decision": "existing", "parent_project_id": "proj_auth"}

    result = plan_and_provision(
        store,
        collab,
        policy,
        "add single sign-on to the auth project",
        hierarchy,
        planner_llm=_planner_llm(plan_reply),
        router_llm=_router_llm(route_reply),
        harness=HarnessType.MOCK.value,
    )
    assert result.route.decision == "existing"
    root = next(p for p in hierarchy.projects if p["id"] == result.root_project_id)
    assert root["parent_project_id"] == "proj_auth"
    assert result.task_count == 1
