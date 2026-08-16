"""Shared vocabulary for the OmniAgentOS Orchestrator ("worker-as-planner" loop).

This module holds ONLY the types, enums and Protocols the orchestrator stages code
against; the stage implementations (:mod:`omniagentos.orchestrator.tiers`,
``.approvals``, ``.executor``, ``.review``, ``.learn``, ``.spec`` and the ``.core``
loop) depend on these, never the reverse. Every external seam is a Protocol/callable
so a test can inject a stub and a full orchestration runs token-free.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    # Imported for annotations only. Kept out of the runtime import graph so
    # ``import omniagentos.orchestrator`` never triggers the intake<->api package
    # import chain (a pre-existing order-sensitive cycle); the real intake modules are
    # imported lazily where they are actually used (stage 1, at run time).
    from omniagentos.intake.fable import Effort
    from omniagentos.intake.planner import Complexity, PlannedTask, ProjectPlan

# ---------------------------------------------------------------------------
# User-controllable knobs
# ---------------------------------------------------------------------------

# The master knob. Drives planner effort, executor tier, and the quality gate.
Priority = Literal["fast", "balanced", "quality"]

# The fusion lane an executor runs in (mirrors the /fusion, /ultrabuild lanes).
FusionLane = Literal["superfast", "fusion", "ultrabuild"]

_VALID_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high", "max"})
_VALID_LANES: frozenset[str] = frozenset({"superfast", "fusion", "ultrabuild"})


@dataclass(slots=True)
class OverridePins:
    """Optional operator pins, honoured OVER the priority-mode defaults.

    Mirrors fusion's ``--pin`` semantics: a valid pin WINS; an impossible pin is
    dropped and its reason surfaced (see :class:`ResolvedExecution.pin_warnings`).

    * ``planner_model`` / ``planner_effort`` pin the PLAN step's Fable model + level.
      ``planner_effort`` is also the "explicit level request" that overrides the
      cheap complexity estimate.
    * ``executor_model_or_lineage`` pins the executor's model/lineage (a cheap
      adapter's model, or a fusion lineage).
    * ``executor_effort`` pins the executor's fusion lane (superfast|fusion|ultrabuild).
    """

    planner_model: str | None = None
    planner_effort: Effort | None = None
    executor_model_or_lineage: str | None = None
    executor_effort: str | None = None

    @classmethod
    def coerce(cls, value: OverridePins | Mapping[str, Any] | None) -> OverridePins:
        if value is None:
            return cls()
        if isinstance(value, OverridePins):
            return value
        return cls(
            planner_model=_opt_str(value.get("planner_model")),
            planner_effort=_opt_str(value.get("planner_effort")),  # type: ignore[arg-type]
            executor_model_or_lineage=_opt_str(value.get("executor_model_or_lineage")),
            executor_effort=_opt_str(value.get("executor_effort")),
        )


def _opt_str(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


# ---------------------------------------------------------------------------
# Executor tiering
# ---------------------------------------------------------------------------


class ExecutorTier(StrEnum):
    """How an executor task actually runs (the token economy, honoured)."""

    # A cheap-model monitored session (the superfast lane).
    CHEAP = "cheap"
    # Fable-led FUSION as a MONITORED session so it shows in the dashboard.
    FUSION = "fusion"
    # Fusion at the ultrabuild tier (a monitored session), for the quality lane.
    FUSION_ULTRA = "fusion_ultra"

    @property
    def is_fusion(self) -> bool:
        return self in (ExecutorTier.FUSION, ExecutorTier.FUSION_ULTRA)


@dataclass(slots=True)
class ResolvedExecution:
    """The concrete plan the knobs + complexity resolved to (auditable)."""

    priority: Priority
    complexity: Complexity
    planner_effort: Effort
    planner_model: str | None
    executor_tier: ExecutorTier
    executor_model: str | None
    executor_lane: FusionLane
    run_quality_gate: bool
    max_retries: int
    pin_warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Approvals (stage 3)
# ---------------------------------------------------------------------------

# AD-15 finance-only approval vocabulary. ``secret`` remains part of the audit
# vocabulary even though secret reads no longer park. ``bank`` is a permanent
# refusal, not an approval that can be satisfied later.
HardStop = Literal["money", "delete", "secret", "customer", "bank"]
TargetScope = Literal[
    "local_temp",
    "production",
    "unresolved",
    "in_granted_scope",
    "none",
]


@dataclass(slots=True)
class ApprovalRequest:
    """One approval an executor raises mid-run, keyed off the action-class."""

    proposed_action: str
    action_class: str = "consequential"
    tool_name: str = ""
    tool_input: Mapping[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    run_id: str | None = None


@dataclass(slots=True)
class ApprovalDecision:
    """The orchestrator's verdict on one :class:`ApprovalRequest`."""

    approved: bool
    escalated: bool
    category: HardStop | None
    reason: str
    request: ApprovalRequest
    notification_id: str | None = None


class ApprovalNotifier(Protocol):
    """Escalation sink for a hard-stop approval (reused notifications system)."""

    def escalate(self, request: ApprovalRequest, category: HardStop) -> str | None: ...


# ---------------------------------------------------------------------------
# Executor spawn (stage 2)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExecutorRequest:
    """Everything one executor spawn needs, with context already injected."""

    task: PlannedTask
    prompt: str
    working_dir: str
    tier: ExecutorTier
    model: str | None
    # ``None`` when the tier was ESCALATED off the resolved tier: the operator's
    # lane/model pins were for the original (cheap) tier, so an escalated request
    # carries no stale pin and the tier's own defaults take over (see
    # ``Orchestrator._execute_task``). ``request.lane`` is advisory metadata only.
    lane: FusionLane | None
    title: str
    budget_usd_max: float | None = None
    attempt: int = 1
    # P3 (FIX 6): the project's validated granted write roots BEYOND working_dir
    # (its ``root_dirs`` + ``allowed_dirs``), resolved SERVER-SIDE at dispatch. The
    # monitored-session runner forwards these to ``spawn(granted_roots=...)`` so an
    # orchestrate-mode executor session is frozen with the SAME project scope an
    # intake session gets -- otherwise it persists NULL granted_roots and can only
    # write inside working_dir. ``None`` == no extra scope (pre-P3 behavior).
    granted_roots: list[str] | None = None
    run_id: str | None = None
    on_spawn: Callable[[str], None] | None = None


@dataclass(slots=True)
class ExecutorResult:
    """The outcome of one executor spawn."""

    status: Literal["ok", "error"]
    output_text: str = ""
    session_id: str | None = None
    error: str | None = None
    commits: list[str] = field(default_factory=list)
    working_dir: str | None = None


class ExecutorRunner(Protocol):
    """Spawns the executor and, mid-run, consults ``gateway`` for each approval.

    The default runners (cheap adapter / fusion monitored session) live in
    :mod:`omniagentos.orchestrator.executor`; tests inject a stub that drives the
    gateway directly so approval routing is exercised without a live process.
    """

    def run(self, request: ExecutorRequest, gateway: ApprovalGatewayProto) -> ExecutorResult: ...


class ApprovalGatewayProto(Protocol):
    """The slice of the approval gateway an executor calls into mid-run."""

    def resolve(self, request: ApprovalRequest) -> ApprovalDecision: ...


# ---------------------------------------------------------------------------
# Quality gate (stage 4)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReviewVerdict:
    """A separate reviewer's verdict against the spec's acceptance criteria.

    THREE-VALUED (H2), matching ``swarm.scheduler.SwarmReviewOutcome``: ``error``
    means the reviewer INFRASTRUCTURE failed (adapter raised, payload unparseable,
    verdict string unrecognised). It is explicitly NOT a deny — the executor's work
    was never judged — and it is never silently upgraded to a confirm. A logged-out
    or quota-dead reviewer CLI must block the task, not approve it.
    """

    verdict: Literal["confirm", "deny", "error"]
    feedback: str = ""
    reviewer: str = ""


class Reviewer(Protocol):
    """A SEPARATE, cross-lineage reviewer (codex-critic/opus-critic pattern)."""

    def review(
        self,
        *,
        task: PlannedTask,
        spec_markdown: str,
        result: ExecutorResult,
    ) -> ReviewVerdict: ...


# ---------------------------------------------------------------------------
# Learners (stage 5)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LearnEvent:
    """Fired on each executor session completion; drives the learners fan-out."""

    session_id: str | None
    task_title: str
    output_text: str
    vault_dir: str


class LearnHook(Protocol):
    """Fire-and-forget learners (session-transcript curation + a vault/brain write)."""

    def __call__(self, event: LearnEvent) -> None: ...


# ---------------------------------------------------------------------------
# Durable orchestration checkpointing
# ---------------------------------------------------------------------------


class OrchestrationCheckpoint(Protocol):
    """Durable transition sink used to resume orchestration after process death."""

    def record_plan(self, run_id: str, plan_json: str, step_titles: list[str]) -> None: ...

    def step_started(self, run_id: str, seq: int, attempts: int) -> None: ...

    def step_session(self, run_id: str, seq: int, session_id: str) -> None: ...

    def step_finished(
        self,
        run_id: str,
        seq: int,
        status: str,
        attempts: int,
        output_tail: str,
    ) -> None: ...


@dataclass(slots=True)
class ResumeStep:
    seq: int
    title: str
    status: str
    session_id: str | None
    attempts: int
    output_tail: str = ""


@dataclass(slots=True)
class ResumeState:
    plan_json: str
    steps: list[ResumeStep]


# ---------------------------------------------------------------------------
# Context-injection seams (stage 2)
# ---------------------------------------------------------------------------


class SkillSearch(Protocol):
    def __call__(self, q: str, *, limit: int = ...) -> list[dict[str, Any]]: ...


class SkillGet(Protocol):
    def __call__(self, skill_id: str) -> dict[str, Any]: ...


class FileLister(Protocol):
    def __call__(self, working_dir: str) -> Sequence[str]: ...


# ---------------------------------------------------------------------------
# Orchestration result
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TaskOutcome:
    """The result of orchestrating one task in the plan."""

    task_title: str
    tier: ExecutorTier
    # ``blocked_on_review`` (H2): the reviewer infrastructure failed, so no verdict
    # exists. Not "done" (nothing was confirmed) and not "denied" (the work was
    # never judged) — it needs an operator, and it must never aggregate as success.
    status: Literal["done", "denied", "failed", "unreviewed", "blocked_on_review"]
    attempts: int
    session_id: str | None = None
    review: ReviewVerdict | None = None
    output_text: str = ""


@dataclass(slots=True)
class OrchestrationResult:
    """The full outcome of one orchestration run (one task = one run)."""

    run_id: str
    goal: str
    plan: ProjectPlan
    resolved: ResolvedExecution
    spec_note_path: str | None
    tasks: list[TaskOutcome]
    approvals: list[ApprovalDecision]
    status: Literal["done", "partial", "failed"]

    @property
    def escalations(self) -> list[ApprovalDecision]:
        return [d for d in self.approvals if d.escalated]


__all__ = [
    "ApprovalDecision",
    "ApprovalGatewayProto",
    "ApprovalNotifier",
    "ApprovalRequest",
    "ExecutorRequest",
    "ExecutorResult",
    "ExecutorRunner",
    "ExecutorTier",
    "FileLister",
    "FusionLane",
    "HardStop",
    "LearnEvent",
    "LearnHook",
    "OrchestrationCheckpoint",
    "OrchestrationResult",
    "OverridePins",
    "Priority",
    "ResumeState",
    "ResumeStep",
    "ResolvedExecution",
    "ReviewVerdict",
    "Reviewer",
    "SkillGet",
    "SkillSearch",
    "TaskOutcome",
]
