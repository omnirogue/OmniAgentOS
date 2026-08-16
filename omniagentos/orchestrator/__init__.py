"""OmniAgentOS Orchestrator — the "worker-as-planner" loop (product centerpiece).

One orchestration run = one goal driven through five stages, each REUSING an existing
module (planner, memory, skills, sessions, fusion, notifications, curator, vault):

    plan -> spec  |  spawn executor (injected context, tiered)  |  approve-safe /
    escalate-hard  |  quality gate (+iterate)  |  learn (fan-out)

The whole thing is a library the runner / a worker can call — see
:func:`run_orchestration`. User-controllable knobs (``priority`` + ``pins``) DRIVE the
tier/model selection; every external seam is injectable so a full run is token-free
under test and N runs are safe concurrently.
"""

from __future__ import annotations

from omniagentos.orchestrator.approvals import (
    ApprovalGateway,
    NotificationEscalator,
    classify_hard_stop,
    resolve_approval,
)
from omniagentos.orchestrator.contracts import (
    ApprovalDecision,
    ApprovalNotifier,
    ApprovalRequest,
    ExecutorRequest,
    ExecutorResult,
    ExecutorRunner,
    ExecutorTier,
    HardStop,
    LearnEvent,
    OrchestrationCheckpoint,
    OrchestrationResult,
    OverridePins,
    Priority,
    ResolvedExecution,
    ResumeState,
    ResumeStep,
    Reviewer,
    ReviewVerdict,
    TaskOutcome,
)
from omniagentos.orchestrator.core import Orchestrator, run_orchestration
from omniagentos.orchestrator.executor import (
    CheapAdapterRunner,
    FusionSessionRunner,
    build_executor_prompt,
)
from omniagentos.orchestrator.review import CrossLineageReviewer
from omniagentos.orchestrator.spec import render_spec_markdown, write_spec_doc
from omniagentos.orchestrator.tiers import resolve_execution

__all__ = [
    "ApprovalDecision",
    "ApprovalGateway",
    "ApprovalNotifier",
    "ApprovalRequest",
    "CheapAdapterRunner",
    "CrossLineageReviewer",
    "ExecutorRequest",
    "ExecutorResult",
    "ExecutorRunner",
    "ExecutorTier",
    "FusionSessionRunner",
    "HardStop",
    "LearnEvent",
    "NotificationEscalator",
    "Orchestrator",
    "OrchestrationCheckpoint",
    "OrchestrationResult",
    "OverridePins",
    "Priority",
    "ResolvedExecution",
    "ResumeState",
    "ResumeStep",
    "ReviewVerdict",
    "Reviewer",
    "TaskOutcome",
    "build_executor_prompt",
    "classify_hard_stop",
    "render_spec_markdown",
    "resolve_approval",
    "resolve_execution",
    "run_orchestration",
    "write_spec_doc",
]
