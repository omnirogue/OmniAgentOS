"""Swarm Mode domain contracts (WP1) — the shared vocabulary for every swarm WP.

One swarm run = a ``swarm_runs`` row + N board cards (membership via
``board_tasks.swarm_run_id``; the 043 lane CHECK is FROZEN, lane stays NULL) +
a ``swarm_deps`` DAG + an ordered ``swarm_attempts`` chain per card. Planner,
scheduler, router, API, and summary all import these types; they must stay in
lock-step with migration 044's CHECK constraints.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Protocol, get_args

from pydantic import BaseModel, Field, model_validator


class SwarmRunStatus(StrEnum):
    """Lifecycle of a swarm run — mirrors the ``swarm_runs.status`` CHECK."""

    QUEUED = "queued"  # fleet admission control: waiting for capacity
    PLANNING = "planning"  # planner building/validating the DAG
    RUNNING = "running"  # coordinator + slot workers active
    MERGING = "merging"  # integration task / final merge in flight
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = frozenset(
    {SwarmRunStatus.COMPLETED, SwarmRunStatus.FAILED, SwarmRunStatus.CANCELLED}
)

# Mirrors the ``swarm_attempts.end_reason`` CHECK in migration 044.
ATTEMPT_END_REASONS: tuple[str, ...] = (
    "completed",
    "review_denied",
    "rate_limited",
    "auth_failed",
    "crashed",
    "timeout",
    "killed",
    "split",
    "rerouted",
    "blocked",
    "budget",
)

RiskClass = Literal["none", "external", "deploy", "destructive"]


class SwarmTaskSpec(BaseModel):
    """One planned unit of work inside a swarm run (a future board card)."""

    id: str
    title: str = ""  # board-card title (WP4 additive)
    description: str = ""  # worker brief body (WP4 additive)
    depends_on: list[str] = Field(default_factory=list)  # SwarmTaskSpec ids
    complexity: str = "standard"  # simple | standard | complex
    risk_class: RiskClass = "none"  # non-claude spawn refuses external/deploy/destructive
    est_manual_minutes: int = 0  # frozen at plan time (anti-gaming)
    est_agent_minutes: int = 0  # frozen at plan time
    owned_paths: list[str] = Field(
        default_factory=list
    )  # workspace-relative; containment-checked by planner
    tier_hint: str | None = None
    acceptance: str = ""  # human-readable done criteria
    verify_command: str = ""  # runnable check; integration task = whole suite
    category: str | None = None  # optional board-taxonomy label (cross-lane metadata; FB4+)


SwarmMode = Literal["swarm", "solo"]


class FormationBinding(BaseModel):
    """Structured formation decision bound to a swarm plan (Phase B / T1).

    Source of truth for implementer pool, reviewer, mechanical gate, and the
    intended topology. Advisory notes in ``assumptions`` may mirror this for
    humans; routing and provision read *this* field, not the note string.
    Optional so pre-Phase-B ``plan_json`` rows still validate.
    """

    id: str
    implementers: list[str] = Field(default_factory=list)
    reviewer: str = ""
    planner: str = ""
    mechanical_gate: bool = True
    confidence: float | None = None
    topology: str | None = None
    reason: str | None = None  # why selected / fallback
    low_confidence: bool = False  # True when conf < threshold (T1-visible)


class SwarmPlan(BaseModel):
    """The validated DAG a run executes — persisted in ``swarm_runs.plan_json``."""

    goal: str = ""
    tasks: list[SwarmTaskSpec] = Field(default_factory=list)
    assumptions: list[str] = Field(
        default_factory=list
    )  # non-blocking clarifications, surfaced in summary
    parallelism_ratio: float | None = None  # Σ est_agent / critical-path est_agent
    integration_task_id: str | None = None  # auto-appended integration task
    mode: SwarmMode = "swarm"  # solo → sequential Orchestrator (WP4 additive)
    version: int = 1  # plan doc version; bumps on split/resize (WP4 additive)
    target_n: int = 1  # clamp(round(ratio), 2, 5); 1 in solo mode (WP4 additive)
    category: str | None = (
        None  # root-goal taxonomy label; fans out to tasks without their own (FB4+)
    )
    formation: FormationBinding | None = None  # Phase B: bound team structure (not notes-only)


# ---------------------------------------------------------------------------
# Planner decision envelope (P0-CONTRACT freeze / R1).
#
# A non-ready decision is a pre-run result: zero execution side effects
# (no swarm_runs row, no board card, no PLAN.md, no formation row). Only
# disposition ``ready`` may carry executable SwarmPlan content.
#
# ``draft`` (additive, 2026-08-10 operator ruling) is a non-ready disposition
# for a plan that is well-formed but owns a bounded NON-FILE resource
# (``resource:<kind>:<name>``) instead of file paths. It is deliberately
# distinct from ``invalid_plan``: the plan is reviewable and re-submittable
# rather than defective, but — like every other non-ready disposition — it
# carries no executable plan content and therefore cannot provision or run.
# ---------------------------------------------------------------------------

PlanDisposition = Literal[
    "ready",
    "needs_clarification",
    "impossible",
    "policy_denied",
    "invalid_plan",
    "planner_unavailable",
    "draft",
]

# Single authority: the Literal args. Do not hand-mirror this tuple elsewhere.
PLAN_DISPOSITIONS: tuple[str, ...] = get_args(PlanDisposition)

READY_PLAN_DISPOSITION: PlanDisposition = "ready"
DRAFT_PLAN_DISPOSITION: PlanDisposition = "draft"


class PlanIssue(BaseModel):
    """One structured problem on a non-ready (or advisory) plan decision."""

    code: str
    message: str
    path: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class SwarmPlanDecision(BaseModel):
    """Typed planner outcome. Only ``ready`` may carry executable plans.

    Model validation rejects plan content on every non-ready disposition. This
    type does not perform execution side effects — consumers must not provision
    on non-ready decisions.
    """

    disposition: PlanDisposition
    plans: list[SwarmPlan] = Field(default_factory=list)
    issues: list[PlanIssue] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    reason: str = ""
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ready_only_carries_plans(self) -> SwarmPlanDecision:
        if self.disposition != READY_PLAN_DISPOSITION and self.plans:
            raise ValueError(
                "only disposition 'ready' may carry executable plans; "
                f"got disposition={self.disposition!r} with {len(self.plans)} plan(s)"
            )
        return self

    @property
    def is_ready(self) -> bool:
        return self.disposition == READY_PLAN_DISPOSITION


# ---------------------------------------------------------------------------
# swarm.event actions — the activity vocabulary (WP6 writes rows in the
# existing events table with kind='swarm.event'; UI + optimizer read them).
# ---------------------------------------------------------------------------

SWARM_EVENT_KIND = "swarm.event"

ACTION_PLAN_CREATED = "plan_created"
ACTION_RUN_STARTED = "run_started"
ACTION_SLOT_OPENED = "slot_opened"
ACTION_TASK_ASSIGNED = "task_assigned"
ACTION_TASK_COMPLETED = "task_completed"
ACTION_REVIEW_CONFIRMED = "review_confirmed"
ACTION_REVIEW_DENIED = "review_denied"
ACTION_PROVIDER_SWITCHED = "provider_switched"
ACTION_RATE_LIMIT = "rate_limit"
ACTION_RATE_LIMIT_STALL = "rate_limit_stall"
ACTION_TASK_SPLIT = "task_split"
ACTION_RESIZE = "resize"
ACTION_TASK_BLOCKED = "task_blocked"
ACTION_APPROVAL_PARKED = "approval_parked"
ACTION_MERGE_STARTED = "merge_started"
ACTION_RUN_COMPLETED = "run_completed"
ACTION_RUN_FAILED = "run_failed"
# Phase-2 worktree isolation (additive, V2 string-event vocabulary):
ACTION_WORKTREE_CREATED = "worktree_created"
ACTION_BRANCH_MERGED = "branch_merged"
ACTION_MERGE_CONFLICT = "merge_conflict"
# M2c: a confirmed task's worktree was LEFT IN PLACE because its unpersisted
# confirmed work could not be salvage-committed — the path needs manual
# recovery (also recorded in the integration task's feedback).
ACTION_WORKTREE_KEPT = "worktree_kept"
# M4c: a wedged in-progress merge (MERGE_HEAD) found in the main workspace at
# coordinator start/resume was aborted best-effort.
ACTION_MERGE_ABORTED = "merge_aborted"
# PKG-REQUEST-SUBTASKS: worker-initiated fan-out. A worker that discovered its
# task decomposes wrote a structured subtasks_request.json; the coordinator
# either registers the validated children via the atomic split machinery
# (REQUESTED) or rejects the request (DENIED, carrying the guard reason) and
# proceeds to the normal review flow. Workers never spawn anything themselves.
ACTION_SUBTASKS_REQUESTED = "subtasks_requested"
ACTION_SUBTASKS_DENIED = "subtasks_denied"
# PKG-INSESSION-FANOUT: the coordinator answered a validated request with a
# LIVE grant instead of a settle-time split — the worker runs the children as
# subagents inside its own session (Task-tool budget enforced server-side by
# the PreToolUse hook); the parent attempt stays the single
# verify/review/merge unit.
ACTION_SUBTASKS_GRANTED = "subtasks_granted"
# Team fleet observability (leader updates + spawn tree — not token streams):
ACTION_LEADER_UPDATE = "leader_update"
ACTION_WORKER_SPAWNED = "worker_spawned"
ACTION_GATE_DEGRADED = "gate_degraded"
# A dollar cap cannot be evaluated because one or more sessions have no price.
# This is a durable warning, distinct from "overshot".
ACTION_BUDGET_UNENFORCEABLE = "budget_unenforceable"
# Multi-bundle group lifecycle (P5 groups; additive group_* actions on the
# existing swarm.event authority — never a second kind registry). Names follow
# the established ACTION_* snake_case convention.
ACTION_GROUP_CREATED = "group_created"
ACTION_GROUP_ACTIVATED = "group_activated"
ACTION_GROUP_COMPLETED = "group_completed"
ACTION_GROUP_FAILED = "group_failed"
ACTION_GROUP_CANCELLED = "group_cancelled"

SWARM_EVENT_ACTIONS: tuple[str, ...] = (
    ACTION_PLAN_CREATED,
    ACTION_RUN_STARTED,
    ACTION_SLOT_OPENED,
    ACTION_TASK_ASSIGNED,
    ACTION_TASK_COMPLETED,
    ACTION_REVIEW_CONFIRMED,
    ACTION_REVIEW_DENIED,
    ACTION_PROVIDER_SWITCHED,
    ACTION_RATE_LIMIT,
    ACTION_RATE_LIMIT_STALL,
    ACTION_TASK_SPLIT,
    ACTION_RESIZE,
    ACTION_TASK_BLOCKED,
    ACTION_APPROVAL_PARKED,
    ACTION_MERGE_STARTED,
    ACTION_RUN_COMPLETED,
    ACTION_RUN_FAILED,
    ACTION_WORKTREE_CREATED,
    ACTION_BRANCH_MERGED,
    ACTION_MERGE_CONFLICT,
    ACTION_WORKTREE_KEPT,
    ACTION_MERGE_ABORTED,
    ACTION_SUBTASKS_REQUESTED,
    ACTION_SUBTASKS_DENIED,
    ACTION_SUBTASKS_GRANTED,
    ACTION_LEADER_UPDATE,
    ACTION_WORKER_SPAWNED,
    ACTION_GATE_DEGRADED,
    ACTION_BUDGET_UNENFORCEABLE,
    ACTION_GROUP_CREATED,
    ACTION_GROUP_ACTIVATED,
    ACTION_GROUP_COMPLETED,
    ACTION_GROUP_FAILED,
    ACTION_GROUP_CANCELLED,
)

# Frozen subset of additive group_* actions (AD-03). Existing actions above are
# unchanged; this tuple is the explicit group vocabulary for generators/tests.
SWARM_GROUP_EVENT_ACTIONS: tuple[str, ...] = (
    ACTION_GROUP_CREATED,
    ACTION_GROUP_ACTIVATED,
    ACTION_GROUP_COMPLETED,
    ACTION_GROUP_FAILED,
    ACTION_GROUP_CANCELLED,
)


class SwarmEmitter(Protocol):
    """Seam for swarm activity emission (WP6 provides the real events-table writer;
    tests and the fake-spawner scheduler inject a recorder)."""

    def emit(self, run_id: str, action: str, payload: dict[str, Any] | None = None) -> None:
        """Record one swarm.event row for ``run_id``. Must never raise into the
        scheduler — emission is best-effort observability, not control flow."""
        ...


__all__ = [
    "ACTION_APPROVAL_PARKED",
    "ACTION_BRANCH_MERGED",
    "ACTION_BUDGET_UNENFORCEABLE",
    "ACTION_GATE_DEGRADED",
    "ACTION_GROUP_ACTIVATED",
    "ACTION_GROUP_CANCELLED",
    "ACTION_GROUP_COMPLETED",
    "ACTION_GROUP_CREATED",
    "ACTION_GROUP_FAILED",
    "ACTION_MERGE_ABORTED",
    "ACTION_MERGE_CONFLICT",
    "ACTION_MERGE_STARTED",
    "ACTION_PLAN_CREATED",
    "ACTION_PROVIDER_SWITCHED",
    "ACTION_RATE_LIMIT",
    "ACTION_RATE_LIMIT_STALL",
    "ACTION_RESIZE",
    "ACTION_REVIEW_CONFIRMED",
    "ACTION_REVIEW_DENIED",
    "ACTION_RUN_COMPLETED",
    "ACTION_RUN_FAILED",
    "ACTION_RUN_STARTED",
    "ACTION_SLOT_OPENED",
    "ACTION_TASK_ASSIGNED",
    "ACTION_TASK_BLOCKED",
    "ACTION_TASK_COMPLETED",
    "ACTION_TASK_SPLIT",
    "ACTION_LEADER_UPDATE",
    "ACTION_WORKER_SPAWNED",
    "ACTION_WORKTREE_CREATED",
    "ACTION_WORKTREE_KEPT",
    "ATTEMPT_END_REASONS",
    "DRAFT_PLAN_DISPOSITION",
    "PLAN_DISPOSITIONS",
    "READY_PLAN_DISPOSITION",
    "SWARM_EVENT_ACTIONS",
    "SWARM_EVENT_KIND",
    "SWARM_GROUP_EVENT_ACTIONS",
    "FormationBinding",
    "PlanDisposition",
    "PlanIssue",
    "SwarmEmitter",
    "SwarmMode",
    "SwarmPlan",
    "SwarmPlanDecision",
    "SwarmRunStatus",
    "SwarmTaskSpec",
    "TERMINAL_RUN_STATUSES",
    "RiskClass",
    "WORKTREE_GITDIR_RULE_LINES",
]

# Brief lines shared by every private-worktree hard-rule block. ONE definition:
# these lines were previously hand-mirrored between `scheduler.build_worker_brief`
# and `spawn._swarm_rules`, and a hand-mirrored pair drifts.
#
# Provenance: coder agents repeatedly destroyed their own worktrees by treating a
# failing git command as a broken repository and running `git init` to "repair"
# it. In a worktree `.git` is a FILE holding a `gitdir:` pointer, so replacing it
# with a directory severs the link — every tracked file then reads as deleted and
# the branch's commits are stranded where the coordinator cannot fetch them. This
# happened five times in one day and cost a manual recovery each time.
# Provenance: a lane blocked by the sandbox from writing the worktree's gitdir
# escalated to GUI automation and sent a Ctrl-Z keystroke to the operator's Notes
# app before being stopped. A sandbox refusal is a boundary, not an obstacle to
# route around — and the blast radius of routing around it lands on the operator's
# unrelated applications, which no worker has any business touching.
_BLOCKED_ESCALATION_RULE_LINES: tuple[str, ...] = (
    "- If an operation is REFUSED or blocked (sandbox, permissions, a lock),",
    "  that is a boundary — report it and stop. NEVER route around it by any",
    "  means outside this working directory: no GUI automation, no synthetic",
    "  keystrokes or mouse events, no AppleScript/osascript, no controlling",
    "  other applications, no editing files elsewhere on the machine. Being",
    "  blocked is a normal, reportable outcome; a partial task with a clear",
    "  reason is worth far more than a completed one that reached outside.",
)

WORKTREE_GITDIR_RULE_LINES: tuple[str, ...] = (
    "- `.git` here is a FILE containing a `gitdir:` pointer, not a directory.",
    "  NEVER run `git init`, and never delete, move, overwrite or replace",
    "  `.git`. Doing so DETACHES this worktree: your files then read as",
    "  deleted and your commits are stranded and unrecoverable by the",
    "  coordinator. If a git command fails, REPORT the error — never try to",
    "  repair it by re-initialising the repository.",
    *_BLOCKED_ESCALATION_RULE_LINES,
)
