"""Frozen cross-package contracts for OmniAgentOS (Wave 0).

Single source of truth for types shared between packages. Owned by the lead;
implementation packages import from here and MUST NOT edit this file — needed
changes are A2A questions to the lead. Blueprint: ~/Desktop/Agent-OS-Build-Plan.md
(revision 2) sections 6 (domain model), 8 (state machines), 9 (agent contract),
10 (vault), 12 (action classes).

TS mirror for the dashboard: dashboard/src/lib/contracts.ts (keep in sync via lead).
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, get_args, runtime_checkable

from pydantic import BaseModel, Field, field_validator, model_validator

SCHEMA_VERSION = 1


def new_id(prefix: str) -> str:
    """Canonical id generator for ALL entities (tasks 'tsk', runs 'run',
    approvals 'apr', artifacts 'art'). Every process uses this — never invent
    a second id scheme (design finding D-002)."""
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


API_HOST = "127.0.0.1"
# OmniAgentOS canonical port; the sibling Omni product uses :8484.
API_PORT = 8485
DASHBOARD_ORIGIN = "http://127.0.0.1:3003"


def utc_now_iso() -> str:
    """Canonical timestamp format for every table, manifest, and note."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def estimate_tokens(text: str) -> int:
    """Canonical fallback token estimator (chars/4). Flag results estimated=True."""
    return max(1, len(text) // 4)


def digest(data: bytes | str) -> str:
    """Canonical content digest: sha256 hex."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def _repo_root() -> str:
    """Repo root = parent of the omniagentos package. Anchor for default paths so
    the runner/API resolve the SAME db/ledger/vault regardless of launch cwd
    (council INT-002/OPS-005: cwd-relative defaults caused silent split-brain)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_db_path() -> str:
    """The control-plane DB for THIS process.

    ``OMNIAGENTOS_DB`` still wins outright — the launchers export it and every
    consumer must open the same file (an env-ignoring campaign branch would
    invent a second, empty database beside the real one).

    Under a campaign the fallback is the campaign DB rather than the
    package-anchored production file (O-16 way 2: the resume sweep opened an
    empty default DB while the campaign DB held the stale runs). The spelling is
    the launcher's — ``scripts/launch-env.sh`` sets
    ``OMNIAGENTOS_DB="$campaign_root/state.sqlite3"`` — so a process that loses
    the variable still resolves the SAME file instead of a second one.
    """
    env_db = os.environ.get("OMNIAGENTOS_DB")
    if env_db:
        return env_db

    # Imported lazily: contracts.py is bottom-layer and imported by nearly
    # everything, and this branch is only reachable when OMNIAGENTOS_DB is unset.
    from omniagentos.runtime_paths import (
        TOKEN_VAR_ENV_KEYS,
        resolve_sim_context_or_none,
        resolve_var_root,
    )

    if resolve_sim_context_or_none() is not None:
        return str(resolve_var_root(env_keys=TOKEN_VAR_ENV_KEYS, leaf=("state.sqlite3",)))

    return os.path.join(_repo_root(), "var", "omniagentos.db")


def default_ledger_dir() -> str:
    env_ledger = os.environ.get("OMNIAGENTOS_LEDGER_DIR")
    if env_ledger:
        return env_ledger

    # Imported lazily: contracts.py is bottom-layer and imported by nearly
    # everything, and this branch is only reachable when the override is unset.
    from omniagentos.runtime_paths import (
        TOKEN_VAR_ENV_KEYS,
        resolve_sim_context_or_none,
        resolve_var_root,
    )

    if resolve_sim_context_or_none() is not None:
        return str(resolve_var_root(env_keys=TOKEN_VAR_ENV_KEYS, leaf=("ledger",)))

    return os.path.join(_repo_root(), "ledger")


def default_vault_dir() -> str:
    env_vault = os.environ.get("OMNIAGENTOS_VAULT_DIR")
    if env_vault:
        return env_vault

    # Imported lazily for the same reason as default_ledger_dir().
    from omniagentos.runtime_paths import (
        TOKEN_VAR_ENV_KEYS,
        resolve_sim_context_or_none,
        resolve_var_root,
    )

    if resolve_sim_context_or_none() is not None:
        return str(resolve_var_root(env_keys=TOKEN_VAR_ENV_KEYS, leaf=("vault",)))

    return os.path.join(_repo_root(), "vault")


def default_policy_path() -> str:
    """Resolve the policy config path independent of the process cwd.

    Env override `OMNIAGENTOS_POLICY` wins; otherwise anchor to the repo root
    (parent of the `omniagentos` package) so the runner/API resolve the real
    configs/policy.yaml no matter what directory they are launched from."""
    override = os.environ.get("OMNIAGENTOS_POLICY")
    if override:
        return override
    return os.path.join(_repo_root(), "configs", "policy.yaml")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskState(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    VALIDATING = "validating"
    REVIEWING = "reviewing"
    REVISION_REQUESTED = "revision_requested"
    COMPLETED = "completed"
    PAUSED = "paused"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYING = "retrying"


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class StepStatus(StrEnum):
    PENDING = "pending"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPENSATED = "compensated"
    SKIPPED = "skipped"


class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ActionClass(StrEnum):
    """Blueprint section 12. Ordering is trust-significant (lowest → highest risk)."""

    READ_ONLY = "read_only"
    SANDBOXED_CREATION = "sandboxed_creation"
    INTERNAL_REVERSIBLE = "internal_reversible"
    EXTERNAL_REVERSIBLE = "external_reversible"
    CONSEQUENTIAL = "consequential"
    # The ONLY class that hard-stops in AUTO mode (AC-policy). Reserved for
    # genuinely irreversible acts: file deletion / destructive file ops / writes
    # outside project scope, and money-movement / payment-processing WRITES. A
    # money READ or an in-scope write is NOT irreversible. See policy.is_hard_stop.
    IRREVERSIBLE = "irreversible"


class Arm(StrEnum):
    """Baseline-ladder arm (blueprint section 11.8)."""

    B0 = "b0"
    B1 = "b1"
    CHAMPION = "champion"


class HarnessType(StrEnum):
    CLI_CLAUDE = "cli-claude"
    CLI_CODEX = "cli-codex"
    CLI_GROK = "cli-grok"
    CLI_KIMI = "cli-kimi"
    CLI_GEMINI = "cli-gemini"
    CLI_QWEN = "cli-qwen"
    MINI_SWE = "mini-swe"
    OPENHANDS = "openhands"
    AGENTDECK = "agentdeck"
    FUSION = "fusion"
    IMPROVE = "improve"
    # Swarm run manifests (WP7 terminal hook -> ledger). Not an adapter — the
    # registry never resolves this member; it identifies swarm-run ledger
    # lines the same way FUSION/IMPROVE identify theirs.
    SWARM = "swarm"
    MOCK = "mock"


class NoteType(StrEnum):
    RUN = "run"
    EXPERIMENT = "experiment"
    LEARNING = "learning"
    FAILURE = "failure"
    DECISION = "decision"
    BENCHMARK = "benchmark"
    DISCIPLINE = "discipline"
    SOURCE = "source"
    TOURNAMENT = "tournament"  # H2 self-improvement lab
    LEADERBOARD = "leaderboard"  # curated log-book of top orchestrations
    PLAYBOOK = "playbook"  # validated-traits playbook
    PROMPT = "prompt"  # versioned system-prompt surface note
    BRIEFING = "briefing"
    BRAND = "brand"  # TN.11 brand/voice pack (voice guide, banned claims)
    CAMPAIGN = "campaign"  # TN.11 campaign context (offer, audience)
    OFFER = "offer"  # TN.11 offer facts a content agent must not invent
    INSIGHT = "insight"  # T6.5 promoted durable lesson


class ResultStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"


class ErrorCode(StrEnum):
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    ERROR = "error"
    UNAUTHORIZED = "unauthorized"
    REQUEST_TOO_LARGE = "request_too_large"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    INVALID_STATE = "invalid_state"
    VALIDATION = "validation"
    POLICY_DENIED = "policy_denied"
    BUDGET_EXCEEDED = "budget_exceeded"
    PAUSED = "paused"
    CONFLICT = "conflict"
    INTERNAL = "internal"


# ---------------------------------------------------------------------------
# Execution envelope (TN.0) — the ONE tier/effort vocabulary.
#
# Before this, "tier" meant seven different things across the codebase and
# reasoning effort was spelled three separate times (modelintel/router.py:32,
# swarm/router.py:90 EFFORT_LEVELS, adapters/common.py:219). Worse, effort
# travelled to adapters through TWO metadata keys that never met:
# metadata["effort"] (written by intake/lab, read ONLY by adapters/claude.py)
# and metadata["reasoning_effort"] (written by swarm, read ONLY by codex/grok).
# Swarm's router-decided effort was therefore silently dropped for every Claude
# worker. These enums are the single source of truth that closes that.
#
# Existing vocabularies (swarm simple/standard/complex, modelintel difficulty,
# cascade rungs) keep working untouched as views over these.
# ---------------------------------------------------------------------------


class ModelTier(StrEnum):
    """Execution tier. Ordering is trust-significant (cheapest -> strongest),
    same convention as ActionClass."""

    CHEAP = "cheap"
    STANDARD = "standard"
    STRONG = "strong"
    MAX = "max"


class ReasoningEffort(StrEnum):
    """The one effort vocabulary. Superset of every existing copy. Ordering is
    significant.

    MAX was added after T4.5 found it live in two internal declarations that this
    enum did not cover -- intake/fable.py:46 Effort and orchestrator/intent.py:24
    (+ _VALID_EFFORTS at :30). Normalizing strictly through a MAX-less enum would
    have silently clamped those to XHIGH: a second, quieter instance of exactly
    the drift bug T4.5 exists to kill. Not every provider accepts every member --
    codex takes 'minimal', others do not, and CLI support for 'max' was NOT
    independently confirmed -- so adapters map per-provider explicitly (see
    adapters/common.py cli_reasoning_effort) rather than passing values through."""

    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class SandboxLevel(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"


class ScopeEnforcement(StrEnum):
    """Rollout lever for declared-output verification. OBSERVE records and emits
    but never blocks; ENFORCE acts on the verdict.

    OBSERVE is the default on every lane, deliberately: every real agent touches
    something it did not declare (a lockfile, a regenerated migration, an
    __init__ export), so shipping ENFORCE hot drives the escalation rate toward
    100%, makes the human queue noise, and gets the feature switched off. This
    is a PER-LANE setting with no global kill switch, for the same reason."""

    OFF = "off"
    OBSERVE = "observe"
    ENFORCE = "enforce"


class AssessmentVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    NEEDS_REVIEW = "needs_review"
    NEEDS_REPLAN = "needs_replan"


class TaskMode(StrEnum):
    """What KIND of output a task produces. Drives which roots get provisioned:
    code edits the repo under a declared file scope; report/content/image/video
    write to artifact roots and never touch the repo; intake_processing reads
    uploads read-only, mutates a workspace, and writes artifacts."""

    CODE = "code"
    REPORT = "report"
    CONTENT = "content"
    IMAGE = "image"
    VIDEO = "video"
    INTAKE_PROCESSING = "intake_processing"


class WorkItemMode(StrEnum):
    """Work-item-level intent constraining which TaskModes are permitted."""

    BUILD = "build"
    REPORT = "report"
    IMAGE = "image"
    CAMPAIGN = "campaign"


class InteractionKind(StrEnum):
    """Mid-run human<->agent channel (TN.10). Lets an operator steer a running
    session without killing it, and lets an agent ask rather than guess."""

    NUDGE = "nudge"
    QUESTION = "question"
    ANSWER = "answer"


class BlockingPolicy(StrEnum):
    """How an interaction interrupts. NONE: delivered opportunistically.
    CHECKPOINT: delivered at the next tool boundary. WAIT: parks the job."""

    NONE = "none"
    CHECKPOINT = "checkpoint"
    WAIT = "wait"


class PlanApprovalState(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# Rank tables live HERE, next to the enums, mirroring runner/core.py's
# _ACTION_CLASS_RANK convention. The monotonic join in policy/execution.py
# depends on them and they must not be redefinable outside this frozen file.
MODEL_TIER_RANK: dict[ModelTier, int] = {
    ModelTier.CHEAP: 0,
    ModelTier.STANDARD: 1,
    ModelTier.STRONG: 2,
    ModelTier.MAX: 3,
}

REASONING_EFFORT_RANK: dict[ReasoningEffort, int] = {
    ReasoningEffort.MINIMAL: 0,
    ReasoningEffort.LOW: 1,
    ReasoningEffort.MEDIUM: 2,
    ReasoningEffort.HIGH: 3,
    ReasoningEffort.XHIGH: 4,
    ReasoningEffort.MAX: 5,
}


# ---------------------------------------------------------------------------
# State machines (blueprint section 8; semantics in contracts/statemachine.md)
# ---------------------------------------------------------------------------

TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.DRAFT: frozenset({TaskState.READY, TaskState.CANCELLED}),
    TaskState.READY: frozenset(
        {TaskState.QUEUED, TaskState.PAUSED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.QUEUED: frozenset(
        {TaskState.RUNNING, TaskState.PAUSED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.AWAITING_APPROVAL,
            TaskState.VALIDATING,
            TaskState.COMPLETED,  # H1 has no reviewer: a run completing projects the task straight to COMPLETED (DV/D-002)
            TaskState.PAUSED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.AWAITING_APPROVAL: frozenset(
        {TaskState.RUNNING, TaskState.PAUSED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.VALIDATING: frozenset(
        {
            TaskState.REVIEWING,  # H2+ path
            TaskState.COMPLETED,  # H1 path: validation passes → task completes (DV/D-002)
            TaskState.PAUSED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.REVIEWING: frozenset(
        {
            TaskState.REVISION_REQUESTED,
            TaskState.COMPLETED,
            TaskState.PAUSED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.REVISION_REQUESTED: frozenset(
        {TaskState.RUNNING, TaskState.PAUSED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.FAILED: frozenset({TaskState.RETRYING}),
    TaskState.RETRYING: frozenset({TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.PAUSED: frozenset({TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}

RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset({RunState.RUNNING, RunState.PAUSED, RunState.CANCELLED}),
    RunState.RUNNING: frozenset(
        {
            RunState.AWAITING_APPROVAL,
            RunState.VALIDATING,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.PAUSED,
        }
    ),
    RunState.AWAITING_APPROVAL: frozenset({RunState.RUNNING, RunState.FAILED, RunState.CANCELLED}),
    # VALIDATING may return to RUNNING when a non-validate step follows a validate
    # group, and may park for approval when a gated step is reached mid-plan.
    # Both were previously written illegally (runner integrity / M-42).
    RunState.VALIDATING: frozenset(
        {
            RunState.RUNNING,
            RunState.AWAITING_APPROVAL,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.PAUSED: frozenset({RunState.QUEUED, RunState.CANCELLED}),
    RunState.COMPLETED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}

TERMINAL_RUN_STATES: frozenset[RunState] = frozenset(
    {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
)


def can_transition_task(current: TaskState, target: TaskState) -> bool:
    return target in TASK_TRANSITIONS[current]


def can_transition_run(current: RunState, target: RunState) -> bool:
    return target in RUN_TRANSITIONS[current]


# ---------------------------------------------------------------------------
# Agent adapter contract (blueprint section 9)
# ---------------------------------------------------------------------------


class BudgetSpec(BaseModel):
    """Per-run budgets. None means unlimited for that dimension."""

    wall_ms_max: int | None = None
    tokens_max: int | None = None
    cost_usd_max: float | None = None
    max_turns: int | None = None


class AgentUsage(BaseModel):
    """Usage accounting. `estimated` MUST be True unless every populated numeric
    field came from the provider's own report (see docs/research/cli-adapters.md:
    claude reports cost+tokens; codex reports tokens only; grok reports neither)."""

    wall_ms: int
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    estimated: bool = True
    source: str = "estimator"  # "cli-report" | "estimator" | "mixed" | "imported"


class HealthStatus(BaseModel):
    healthy: bool
    detail: str = ""
    capabilities: dict[str, bool] = Field(default_factory=dict)


class HarnessProfile(BaseModel):
    """Recorded on EVERY run (blueprint section 11.8). env_hash pins the execution
    environment (see omniagentos/harnesses: hash of resolved package versions +
    python version + tool versions)."""

    harness: HarnessType
    version: str = ""
    env_hash: str = ""
    params: dict[str, Any] = Field(default_factory=dict)


class Receipt(BaseModel):
    """Adapter-emitted side-effect receipt (an adapter reporting an external action
    it took). Rides on AgentResult.receipts. Distinct from IdempotencyReceipt, which
    is the runner's registry row surfaced on runs/manifests (DV-001)."""

    key: str
    action: str
    target: str = ""
    at: str = Field(default_factory=utc_now_iso)
    result_digest: str | None = None


class IdempotencyReceipt(BaseModel):
    """The runner's idempotency-registry row (one per effect step). This is the
    receipt shape surfaced EVERYWHERE the run's receipts are shown — RunManifest,
    GET /api/runs/{id}, the dashboard, and Store.idem_for_run all use exactly these
    fields (DV-001). Mirrors the `idempotency` table + dashboard Receipt type."""

    key: str
    run_id: str
    step_name: str
    result_json: str | None = None
    created_at: str
    completed_at: str | None = None


class ExecutionEnvelope(BaseModel):
    """What the deterministic policy decided, and WHY.

    Carried on AgentInput so the adapter, the telemetry row and the audit trail
    all read the SAME decision instead of three re-derivations. `reasons` is not
    decoration: the policy is monotonic (it can only ratchet a task UP a tier or
    effort, never down), and each ratchet appends a human-readable reason, so a
    surprising spend is explainable after the fact."""

    tier: ModelTier | None = None
    effort: ReasoningEffort | None = None
    max_tool_turns: int | None = None  # advisory mirror of BudgetSpec.max_turns
    sandbox_level: SandboxLevel = SandboxLevel.READ_ONLY
    extra_dirs: list[str] = Field(default_factory=list)
    scope_enforcement: ScopeEnforcement = ScopeEnforcement.OFF
    reasons: list[str] = Field(default_factory=list)
    policy_version: str = ""


class DeclaredScope(BaseModel):
    """What a mutating job says it will touch, declared BEFORE it runs.

    PRESENCE IS THE SWITCH, and the distinction is load-bearing:
      scope is None            -> verification DISABLED (classify_risk gets
                                  declared_paths=None). This is the default for
                                  every existing call site, which is what makes
                                  the field additive and backward compatible.
      scope present, all empty -> declared_paths=[] -> ANY changed path is a
                                  mismatch. That is the correct declaration for a
                                  read-only analysis job, and it is a safe
                                  default, not a bug.
    There is deliberately no third 'advisory' encoding: observe-vs-enforce lives
    on ExecutionEnvelope.scope_enforcement, and mixing the two would give four
    states where two suffice."""

    files_to_modify: list[str] = Field(default_factory=list)
    files_to_create: list[str] = Field(default_factory=list)
    files_to_delete: list[str] = Field(default_factory=list)
    create_roots: list[str] = Field(default_factory=list)
    must_modify: list[str] = Field(default_factory=list)
    confident: bool = False  # planner asserts the scope is complete
    verify_command: str | None = None


class ObservedChange(BaseModel):
    """What the working tree ACTUALLY shows. NEVER agent-authored.

    `source` is load-bearing rather than informational: 'unobserved' must not be
    read as 'nothing changed'. Inconclusive is not clean, and the assessment
    ladder treats it as NEEDS_REVIEW rather than PASS."""

    source: str = "unobserved"  # git-worktree | git-index | agent-report | unobserved
    base_ref: str | None = None
    head_ref: str | None = None
    paths: list[str] = Field(default_factory=list)
    status_by_path: dict[str, str] = Field(default_factory=dict)  # path -> A|M|D|R|C|T
    truncated: bool = False


class ScopeVerdictModel(BaseModel):
    """Serializable declared-vs-actual verdict.

    execution_level is DELIBERATELY NOT reliability RiskResult.level. That one
    folds in a keyword scan of diff CONTENT (billing|payment|auth|delet|secret)
    and forces L4 — correct when an AI patches our own source, catastrophic for
    ordinary task execution where most real diffs match 'delet' or 'auth'. Here
    the keyword hit is ADVISORY (recorded below, used only to pick the assessor
    rung). Never pass this value into the reliability pipeline's quorum."""

    ok: bool = True
    enforcement: ScopeEnforcement = ScopeEnforcement.OFF
    execution_level: int = 1  # 1-4; NOT RiskResult.level
    tier: str | None = None  # 'P' | 'S' | None
    undeclared: list[str] = Field(default_factory=list)
    missing_creates: list[str] = Field(default_factory=list)
    missing_must_modify: list[str] = Field(default_factory=list)
    byproducts: list[str] = Field(default_factory=list)
    keyword_hit: bool = False  # advisory in this lane
    reasons: list[str] = Field(default_factory=list)


class AgentInput(BaseModel):
    run_id: str
    task_id: str
    prompt: str
    working_dir: str | None = None
    model: str | None = None
    output_schema: dict[str, Any] | None = None  # JSON Schema for structured output
    tools_allowed: list[str] = Field(default_factory=list)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # --- TN.0 additive, defaulted: all 22 existing construction sites are
    # unaffected. envelope defaults to an all-None envelope whose sandbox_level
    # is the safe READ_ONLY and whose enforcement is OFF; scope defaults to None,
    # which is exactly the "verification disabled" semantics governance already
    # defines. Adapters should migrate from metadata[...] string lookups to these
    # typed fields; metadata stays for genuinely per-adapter passthrough.
    # NOTE: runner/core.py:_strip_elevation_metadata fail-closes model-authored
    # elevation flags. These typed fields must NOT become a new smuggling path —
    # sandbox_level and extra_dirs are set by the runner, never by a plan.
    envelope: ExecutionEnvelope = Field(default_factory=ExecutionEnvelope)
    scope: DeclaredScope | None = None


class AgentResult(BaseModel):
    status: ResultStatus
    output_text: str = ""
    output_json: dict[str, Any] | None = None  # validated against output_schema when given
    session_ref: str | None = None  # provider resume handle (session_id/thread_id)
    usage: AgentUsage
    receipts: list[Receipt] = Field(default_factory=list)
    log_path: str | None = None
    error: str | None = None
    # --- TN.0 additive: observed is what the tree shows, scope_verdict is the
    # declared-vs-actual result. Both default to None so every existing adapter
    # keeps constructing unchanged. "The agent said it did the work" is not
    # evidence; these two fields are where the evidence goes.
    observed: ObservedChange | None = None
    scope_verdict: ScopeVerdictModel | None = None


@runtime_checkable
class AgentAdapter(Protocol):
    """Provider-neutral worker contract. Implementations: omniagentos/adapters
    (cli-claude, cli-codex, cli-grok), omniagentos/harnesses (mini-swe, openhands),
    omniagentos/mock_adapter.py (tests)."""

    name: str
    version: str

    def run(self, input: AgentInput) -> AgentResult: ...

    def cancel(self, session_ref: str) -> bool: ...

    def health(self) -> HealthStatus: ...


# ---------------------------------------------------------------------------
# Ledger manifest (one JSONL line per run) — blueprint Epic G / section 11.8
# ---------------------------------------------------------------------------


class RunManifest(BaseModel):
    """One JSONL line per run, written EXACTLY ONCE at run finalization (after the
    terminal state and usage rollups are persisted — design finding D-007).
    `state` is therefore always a member of TERMINAL_RUN_STATES."""

    schema_version: int = SCHEMA_VERSION
    run_id: str
    task_id: str
    discipline: str | None = None
    arm: Arm | None = None
    harness: HarnessProfile
    agent: str | None = None
    model: str | None = None
    state: RunState
    started_at: str | None = None
    finished_at: str | None = None
    usage: AgentUsage | None = None
    receipts: list[IdempotencyReceipt] = Field(
        default_factory=list
    )  # from Store.idem_for_run (G1/B1, DV-001)
    output_digest: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    vault_note: str | None = None
    trace_id: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Vault frontmatter (blueprint section 10) — exact field set is a G1 criterion
# ---------------------------------------------------------------------------


class VaultFrontmatter(BaseModel):
    id: str
    type: NoteType
    discipline: str | None = None
    created: str = Field(default_factory=utc_now_iso)
    source_run: str | None = None
    confidence: str | None = None  # "low" | "medium" | "high"
    status: str = "active"  # "active" | "superseded" | "draft"
    supersedes: str | None = None


# ---------------------------------------------------------------------------
# SSE event names (wire format in contracts/events.md)
# ---------------------------------------------------------------------------


class Events:
    RUN_UPDATED = "run.updated"
    STEP_UPDATED = "step.updated"
    TASK_UPDATED = "task.updated"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    PAUSE_CHANGED = "pause.changed"
    AUDIT = "audit.event"
    WORKER_HEARTBEAT = "worker.heartbeat"
    COMMS_MESSAGE = "comms.message"
    GOAL_METRIC = "goal.metric"
    ALERT_CREATED = "alert.created"
    BRIEFING_READY = "briefing.ready"
    SUGGESTION_UPDATED = "suggestion.updated"
    SESSION_UPDATED = "session.updated"

    ALL: tuple[str, ...] = (
        RUN_UPDATED,
        STEP_UPDATED,
        TASK_UPDATED,
        APPROVAL_REQUESTED,
        APPROVAL_DECIDED,
        PAUSE_CHANGED,
        AUDIT,
        WORKER_HEARTBEAT,
        COMMS_MESSAGE,
        GOAL_METRIC,
        ALERT_CREATED,
        BRIEFING_READY,
        SUGGESTION_UPDATED,
        SESSION_UPDATED,
    )
    # WORKER_HEARTBEAT is an SSE-ONLY event synthesized by the API from the
    # heartbeats table (<=1 per 15s). It is NEVER written to the events table
    # (design finding D-010: heartbeat floods would drown the audit log and
    # evict real events from the SSE replay window).
    #
    # SESSION_UPDATED (W1) is likewise SSE-ONLY, synthesized by the API from the
    # sessions table on each poll — it is NEVER written to the events table
    # (same flood/eviction reasoning as WORKER_HEARTBEAT; council T-DESIGN-002).
    # Session LIFECYCLE transitions (created/awaiting_approval/completed/killed)
    # ARE persisted, but as Events.AUDIT rows, not as session.* types.
    SSE_ONLY: tuple[str, ...] = (WORKER_HEARTBEAT, SESSION_UPDATED)


# ---------------------------------------------------------------------------
# Mission additive SSE vocabulary (P0-CONTRACT freeze, AD-03).
#
# These ride the existing events table / GET /api/events stream as plain string
# kinds — the same additive pattern as reliability V2 and swarm.event. They are
# deliberately NOT members of Events.ALL (that frozen Wave-0 tuple is untouched).
# ---------------------------------------------------------------------------


class MissionEvents:
    """Additive mission command-center event kinds (not in Events.ALL)."""

    CHAT_PROJECT_BINDING_CHANGED = "chat.project_binding_changed"
    CLASSIFICATION_UPDATED = "classification.updated"
    CLASSIFICATION_NEEDS_CONFIRMATION = "classification.needs_confirmation"
    CLASSIFICATION_SHADOW_COMPARED = "classification.shadow_compared"
    CONTEXT_PACKAGE_READY = "context.package_ready"
    CONTEXT_DELIVERY_FAILED = "context.delivery_failed"
    TASK_CONTRACT_CREATED = "task_contract.created"
    TASK_CONTRACT_UPDATED = "task_contract.updated"
    TASK_CONTRACT_WOULD_DENY = "task_contract.would_deny"
    CONTRACT_GATE_UPDATED = "contract_gate.updated"
    CONTRACT_BUDGET_UPDATED = "contract_budget.updated"
    RESOURCE_REQUEST_CREATED = "resource_request.created"
    RESOURCE_REQUEST_UPDATED = "resource_request.updated"
    FORMATION_UPDATED = "formation.updated"
    VERIFICATION_UPDATED = "verification.updated"
    RECEIPT_AVAILABLE = "receipt.available"
    MEMORY_UPDATED = "memory.updated"

    ALL: tuple[str, ...] = (
        CHAT_PROJECT_BINDING_CHANGED,
        CLASSIFICATION_UPDATED,
        CLASSIFICATION_NEEDS_CONFIRMATION,
        CLASSIFICATION_SHADOW_COMPARED,
        CONTEXT_PACKAGE_READY,
        CONTEXT_DELIVERY_FAILED,
        TASK_CONTRACT_CREATED,
        TASK_CONTRACT_UPDATED,
        TASK_CONTRACT_WOULD_DENY,
        CONTRACT_GATE_UPDATED,
        CONTRACT_BUDGET_UPDATED,
        RESOURCE_REQUEST_CREATED,
        RESOURCE_REQUEST_UPDATED,
        FORMATION_UPDATED,
        VERIFICATION_UPDATED,
        RECEIPT_AVAILABLE,
        MEMORY_UPDATED,
    )


# ---------------------------------------------------------------------------
# Transcript → Jira program SSE vocabulary (BP-A2, jira-goals Wave 2).
#
# Same additive pattern as MissionEvents: plain string kinds riding the existing
# events table / GET /api/events stream, deliberately NOT members of Events.ALL
# (that frozen Wave-0 tuple stays untouched) and NOT members of
# MissionEvents.ALL (that tuple is the mission command-center's own freeze).
#
# These three names are the program's contract — downstream lanes EMIT them:
#   company_goal.updated        — a company goal was created / patched / relinked
#   employee_transcript.updated — a transcript for an employee landed or changed
#   recommendation.updated      — a Jira recommendation was produced or decided
# ---------------------------------------------------------------------------


class JiraGoalsEvents:
    """Additive transcript→Jira event kinds (not in Events.ALL)."""

    COMPANY_GOAL_UPDATED = "company_goal.updated"
    EMPLOYEE_TRANSCRIPT_UPDATED = "employee_transcript.updated"
    RECOMMENDATION_UPDATED = "recommendation.updated"

    ALL: tuple[str, ...] = (
        COMPANY_GOAL_UPDATED,
        EMPLOYEE_TRANSCRIPT_UPDATED,
        RECOMMENDATION_UPDATED,
    )


# ---------------------------------------------------------------------------
# Reliability / mission correlation contracts (P0-CONTRACT freeze, §4.1).
# Shapes only — no storage, writers, or runtime wiring in this slice.
# ---------------------------------------------------------------------------

# 1 USD = 1_000_000_000 nano-USD. Integer enforcement unit for exact cost.
# Implemented as plain int string arithmetic so results never depend on the
# active decimal.Context precision/rounding.
NANO_USD_SCALE = 1_000_000_000
NANO_USD_DIGITS = 9
# Exact decimal text: no whitespace, no scientific notation, no leading '+'.
_COST_DECIMAL_RE = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")

ProviderRequestState = Literal["not_sent", "sent", "indeterminate"]
PROVIDER_REQUEST_STATES: tuple[str, ...] = get_args(ProviderRequestState)


def _require_strict_nonneg_int(value: object, *, field: str) -> int:
    """Accept only real non-negative ints (reject bool/str/float before coerce)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a non-negative int (got {type(value).__name__})")
    if value < 0:
        raise ValueError(f"{field} must be >= 0")
    return value


def _require_strict_nonneg_int_or_none(value: object, *, field: str) -> int | None:
    if value is None:
        return None
    return _require_strict_nonneg_int(value, field=field)


class CostQuality(StrEnum):
    """Dimension-specific cost quality (never mixed with token estimation)."""

    EXACT = "exact"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class ProviderCallStage(StrEnum):
    """Stage of a provider call observation (one initiation → one observation)."""

    PLANNER = "planner"
    CLARIFIER = "clarifier"
    PLANNER_RETRY = "planner_retry"
    WORKER = "worker"
    WORKER_RETRY = "worker_retry"
    REVIEWER = "reviewer"
    REVIEWER_RETRY = "reviewer_retry"
    ESCALATION = "escalation"
    INTEGRATOR = "integrator"
    INTEGRATOR_RETRY = "integrator_retry"


def _parse_cost_usd_decimal(text: str) -> tuple[str, int]:
    """Parse exact USD decimal text → (exact_text, nano_usd).

    Uses integer string arithmetic only — independent of ``decimal.Context``
    precision/rounding. Preserves caller text byte-for-byte (no float, no
    re-format, no strip). Whitespace is rejected rather than normalized.
    At most 9 fractional digits (nano-USD); more is rejected.
    """
    if not isinstance(text, str) or text == "":
        raise ValueError("cost decimal text must be a non-empty string")
    if any(ch.isspace() for ch in text):
        raise ValueError(f"cost decimal text must not contain whitespace: {text!r}")
    if not _COST_DECIMAL_RE.match(text):
        raise ValueError(f"cost decimal text is not a plain decimal: {text!r}")
    if "." in text:
        whole_s, frac_s = text.split(".", 1)
    else:
        whole_s, frac_s = text, ""
    if len(frac_s) > NANO_USD_DIGITS:
        raise ValueError(
            f"cost decimal {text!r} is not representable in integer nano-USD "
            f"(more than {NANO_USD_DIGITS} fractional digits)"
        )
    # Pad fractional part to exactly 9 digits with trailing zeros (not truncation).
    frac_padded = frac_s.ljust(NANO_USD_DIGITS, "0")
    # int() on digit strings is exact and context-independent for any magnitude.
    nano = int(whole_s) * NANO_USD_SCALE + int(frac_padded or "0")
    return text, nano


class ExecutionRef(BaseModel):
    """Correlation envelope only — never an execution state machine.

    Realized as (task_contracts.id, events.execution_id). Lane stores (Swarm,
    Runner, sessions, orchestration, Lab) remain the only execution authorities.
    No ``executions`` table, no registry.
    """

    request_id: str
    execution_id: str
    company_id: str | None = None
    project_id: str | None = None
    campaign_id: str | None = None
    idempotency_key_hash: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)


class EffectiveRoute(BaseModel):
    """Total record of a model-route decision (intended → effective).

    Transport is explicit and NEVER inferred from billing provider or model
    family. Model identity = ``(billing_provider, effective_model)`` at the
    call edge; ``requested_model`` preserves what the caller asked for.
    First production writer targets ``routing_history`` (086) in P4-ROUTING.
    """

    role: str
    requested_model: str
    effective_model: str
    model_lineage: str
    billing_provider: str
    transport: str
    adapter_key: str
    effort: ReasoningEffort | None = None
    profile_id: str | None = None
    profile_revision: str | int | None = None
    selection_reason: str
    price_revision: str | None = None


class CostObservation(BaseModel):
    """One provider-call cost observation (contract only; no storage here).

    Canonical names from issue-02 / §5.4. ``request_id`` and ``execution_id``
    are allocated before every possible provider call (including planner).
    Exact costs round-trip as decimal text without float coercion; integer
    enforcement unit is nano-USD (``cost_usd_nanos``).

    Quality invariants:
    - ``exact`` requires ``cost_usd_decimal`` (+ reconciled ``cost_usd_nanos``)
    - ``estimated`` requires ``cost_upper_bound_usd_nanos`` and forbids exact cost
    - ``unknown`` cannot carry invented exact cost
    """

    call_id: str
    request_id: str
    execution_id: str
    run_id: str | None = None
    campaign_id: str | None = None
    reservation_id: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    session_id: str | None = None
    work_id: str | None = None
    root_trace_id: str | None = None
    stage: ProviderCallStage
    attempt_index: int = 0
    provider: str
    transport: str
    requested_model: str
    effective_model: str
    model_lineage: str
    billing_provider: str
    adapter_key: str
    provider_request_id: str | None = None
    request_state: ProviderRequestState
    provider_outcome: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd_decimal: str | None = None
    cost_usd_nanos: int | None = None
    cost_upper_bound_usd_nanos: int | None = None
    cost_quality: CostQuality
    cost_source: str
    pricing_revision: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    settled_at: str | None = None

    @field_validator("cost_usd_decimal")
    @classmethod
    def _validate_cost_text(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("cost_usd_decimal must be str or None")
        text, _ = _parse_cost_usd_decimal(v)
        return text

    @field_validator(
        "cost_usd_nanos",
        "cost_upper_bound_usd_nanos",
        mode="before",
    )
    @classmethod
    def _validate_nonneg_nano(cls, v: object) -> int | None:
        return _require_strict_nonneg_int_or_none(v, field="nano-USD field")

    @field_validator(
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "attempt_index",
        mode="before",
    )
    @classmethod
    def _validate_nonneg_int(cls, v: object) -> int | None:
        return _require_strict_nonneg_int_or_none(v, field="count field")

    @model_validator(mode="after")
    def _cross_check_cost(self) -> CostObservation:
        text = self.cost_usd_decimal
        nano = self.cost_usd_nanos

        # Reconcile exact decimal text with integer nano-USD.
        if text is not None and nano is None:
            _, computed = _parse_cost_usd_decimal(text)
            object.__setattr__(self, "cost_usd_nanos", computed)
            nano = computed
        elif text is not None and nano is not None:
            _, computed = _parse_cost_usd_decimal(text)
            if computed != nano:
                raise ValueError(
                    f"cost_usd_decimal {text!r} (nanos={computed}) disagrees with "
                    f"cost_usd_nanos={nano}"
                )
        elif text is None and nano is not None:
            # Nanos without authoritative decimal text is not an exact provider
            # report — quality rules below decide whether this is allowed.
            pass

        quality = self.cost_quality
        if quality is CostQuality.EXACT or quality == CostQuality.EXACT:
            if text is None:
                raise ValueError("cost_quality='exact' requires cost_usd_decimal")
            if self.cost_usd_nanos is None:
                raise ValueError("cost_quality='exact' requires cost_usd_nanos")
        elif quality is CostQuality.ESTIMATED or quality == CostQuality.ESTIMATED:
            if self.cost_upper_bound_usd_nanos is None:
                raise ValueError("cost_quality='estimated' requires cost_upper_bound_usd_nanos")
            if text is not None or nano is not None:
                raise ValueError(
                    "cost_quality='estimated' must not carry exact cost fields "
                    "(cost_usd_decimal / cost_usd_nanos)"
                )
        elif quality is CostQuality.UNKNOWN or quality == CostQuality.UNKNOWN:
            if text is not None or nano is not None:
                raise ValueError(
                    "cost_quality='unknown' cannot carry invented exact cost "
                    "(cost_usd_decimal / cost_usd_nanos must be None)"
                )
        return self


# ---------------------------------------------------------------------------
# Decision models for the pure-function packages (design findings D-001/D-005)
# ---------------------------------------------------------------------------


class PolicyDecision(BaseModel):
    requires_approval: bool
    always_human: bool = False
    reason: str = ""


class BudgetDecision(BaseModel):
    allowed: bool
    reason: str = ""


class SandboxSpec(BaseModel):
    """Concrete per-CLI privilege selection derived from tools_allowed (D-005).
    The mapping table is frozen in configs/policy.yaml + contracts/interfaces.md;
    adapters MUST launch their subprocess with exactly this level."""

    level: str  # "read_only" | "workspace_write"
    detail: str = ""


# ---------------------------------------------------------------------------
# Store protocol — the runner/API's SINGLE persistence seam (design D-001/D-ALT)
# ---------------------------------------------------------------------------
#
# Implemented over SQLite by p01 (omniagentos/db/store.py, class SqliteStore).
# The runner and API depend ONLY on this Protocol; leaf packages (policy, budget,
# ledger, vault) are pure functions and never touch the database directly.
# Row dicts use the exact column names from contracts/schema.sql.
#
# Concurrency contract: claim_next_run/reclaim_stale_runs use BEGIN IMMEDIATE;
# every write that carries expect_worker applies "... AND worker_id = ?" and
# returns False on zero rows — the caller MUST treat False as "run reclaimed,
# abort this run's loop" (fencing, design finding D-006).


@runtime_checkable
class Store(Protocol):
    # --- runs ---
    def enqueue_run(self, row: dict[str, Any]) -> None: ...

    def claim_next_run(self, worker_id: str) -> dict[str, Any] | None: ...

    def reclaim_stale_runs(self, worker_id: str, stale_s: int) -> list[dict[str, Any]]: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def update_run(
        self, run_id: str, fields: dict[str, Any], expect_worker: str | None = None
    ) -> bool: ...

    def list_runs(self, filters: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]: ...

    def request_cancel(self, run_id: str) -> bool: ...

    def requeue_paused_runs(self) -> list[str]: ...

    # --- steps ---
    def upsert_step(
        self, run_id: str, seq: int, fields: dict[str, Any], expect_worker: str | None = None
    ) -> bool: ...

    def get_steps(self, run_id: str) -> list[dict[str, Any]]: ...

    # --- idempotency receipts ---
    def idem_insert(self, key: str, run_id: str, step_name: str) -> bool: ...

    def idem_get(self, key: str) -> dict[str, Any] | None: ...

    def idem_complete(self, key: str, result_json: str) -> None: ...

    def idem_for_run(self, run_id: str) -> list[dict[str, Any]]: ...

    # --- tasks ---
    def create_task(self, row: dict[str, Any]) -> None: ...

    def get_task(self, task_id: str) -> dict[str, Any] | None: ...

    def update_task_state(
        self, task_id: str, target: str, expect: list[str] | None = None
    ) -> bool: ...

    def list_tasks(self, filters: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]: ...

    # --- events (audit + SSE feed; NEVER worker.heartbeat rows) ---
    def insert_event(
        self,
        type: str,
        actor: str,
        action: str,
        target_type: str = "",
        target_id: str = "",
        payload: dict[str, Any] | None = None,
        trace_id: str = "",
        execution_id: str = "",
    ) -> int:
        """Insert one event row; NEVER worker.heartbeat.

        ``execution_id`` (migration 086) correlates every event that belongs to
        one lane execution (a run, a session, a longhaul task attempt, ...).
        When non-empty, implementations MUST also populate ``events.sequence``
        with the next gap-free, per-``execution_id`` integer (1, 2, 3, ...),
        computed atomically inside the same write transaction as the INSERT so
        concurrent writers on the same ``execution_id`` never race or leave a
        gap. An empty ``execution_id`` (the default) leaves both columns NULL
        -- not every event has a lane execution to correlate with, and W2.6
        only requires >95% coverage, not 100%.
        """
        ...

    def get_events_after(
        self, after_id: int, types: list[str] | None = None, limit: int = 500
    ) -> list[dict[str, Any]]: ...

    def latest_event_id(self) -> int: ...

    # --- approvals ---
    def create_approval(self, row: dict[str, Any]) -> None: ...

    def get_approval_for(self, run_id: str, step_seq: int | None) -> dict[str, Any] | None: ...

    def decide_approval(
        self, approval_id: str, state: str, decided_by: str, note: str | None = None
    ) -> bool: ...

    def void_pending_approvals(self, run_id: str, note: str) -> int: ...

    def list_approvals(
        self, state: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]: ...

    # --- pause / heartbeats ---
    def get_pause(self) -> dict[str, Any]: ...

    def set_pause(self, paused: bool, reason: str = "") -> dict[str, Any]: ...

    def upsert_heartbeat(self, worker_id: str, pid: int, current_run_id: str | None) -> None: ...

    def get_heartbeats(self) -> list[dict[str, Any]]: ...

    # --- budgets / artifacts / disciplines ---
    def get_budget(self, budget_id: str) -> dict[str, Any] | None: ...

    def upsert_budget_usage(
        self, budget_id: str, wall_ms: int, tokens: int, cost_usd: float
    ) -> None: ...

    def list_budgets(self) -> list[dict[str, Any]]: ...

    def add_artifact(self, row: dict[str, Any]) -> None: ...

    def get_artifacts(self, run_id: str) -> list[dict[str, Any]]: ...

    def list_disciplines(self) -> list[dict[str, Any]]: ...

    def create_discipline(self, row: dict[str, Any]) -> bool: ...
