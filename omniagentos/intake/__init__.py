"""Intake loop: voice/chat request -> clarifying agent -> live board_task + run.

The operator speaks or types a rough request. A cheap clarifying agent refines it
into a precise task spec (asking up to a couple of rounds of targeted questions),
the confirmed spec becomes BOTH a self-assignable board card AND a control-plane
task+run, and the live kanban reconciles each card's column (To-Do / In-Progress /
Done) from its linked run's state as the runner's agent executes it.
"""

from __future__ import annotations

from omniagentos.intake.contracts import (
    MAX_CLARIFY_ROUNDS,
    ClarifyResult,
    RefinedSpec,
)
from omniagentos.intake.planner import (
    HierarchyDAL,
    ProjectPlan,
    RouteDecision,
    estimate_complexity,
    plan_and_provision,
    plan_goal,
    provision_plan,
    route_project,
)
from omniagentos.intake.service import (
    clarify_intake,
    default_clarify_llm,
    dispatch_spec,
    reconcile_board,
    resume_orchestration,
    resume_orphaned_orchestrations,
)

__all__ = [
    "MAX_CLARIFY_ROUNDS",
    "ClarifyResult",
    "HierarchyDAL",
    "ProjectPlan",
    "RefinedSpec",
    "RouteDecision",
    "clarify_intake",
    "default_clarify_llm",
    "dispatch_spec",
    "estimate_complexity",
    "plan_and_provision",
    "plan_goal",
    "provision_plan",
    "reconcile_board",
    "resume_orchestration",
    "resume_orphaned_orchestrations",
    "route_project",
]
