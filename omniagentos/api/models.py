"""Pydantic models used by the HTTP control plane.

The database seam intentionally returns dictionaries.  These models document the
wire shape while route code keeps the database JSON columns as JSON strings.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from omniagentos.contracts import (
    ActionClass,
    ApprovalState,
    Arm,
    BudgetSpec,
    ErrorCode,
    HarnessType,
    RunState,
    StepStatus,
    TaskState,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ErrorBody(ApiModel):
    code: ErrorCode
    message: str
    detail: Any | None = None


class ErrorResponse(ApiModel):
    error: ErrorBody


class Discipline(ApiModel):
    id: str
    name: str
    metric_contract: str = "{}"
    status: str = "active"
    created_at: str


class Task(ApiModel):
    id: str
    discipline_id: str | None = None
    title: str
    input_json: str = "{}"
    acceptance_json: str = "{}"
    state: TaskState
    risk: str = "low"
    created_at: str
    updated_at: str


class RunSummary(ApiModel):
    id: str
    task_id: str
    state: RunState
    harness: HarnessType
    arm: Arm | None = None
    model: str | None = None
    agent: str | None = None
    queued_at: str
    started_at: str | None = None
    finished_at: str | None = None
    cost_usd: float | None = None
    usage_estimated: bool = True


class Run(ApiModel):
    id: str
    task_id: str
    discipline_id: str | None = None
    arm: Arm | None = None
    harness: HarnessType
    harness_version: str = ""
    env_hash: str = ""
    harness_params: str = "{}"
    agent: str | None = None
    model: str | None = None
    attempt: int = 1
    state: RunState
    worker_id: str | None = None
    plan_json: str = "[]"
    output_text: str | None = None
    output_json: str | None = None
    error: str | None = None
    wall_ms: int | None = None
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    usage_estimated: bool = True
    usage_source: str = "estimator"
    budget_json: str = "{}"
    cancel_requested: int = 0
    trace_id: str
    session_ref: str | None = None
    manifest_path: str | None = None
    vault_note_path: str | None = None
    queued_at: str
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str
    updated_at: str


class Step(ApiModel):
    id: int | None = None
    run_id: str
    seq: int
    name: str
    action_class: ActionClass = ActionClass.SANDBOXED_CREATION
    status: StepStatus = StepStatus.PENDING
    checkpoint_json: str = "{}"
    result_json: str | None = None
    error: str | None = None
    idempotency_key: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class Approval(ApiModel):
    id: str
    run_id: str | None = None
    task_id: str | None = None
    step_seq: int | None = None
    action_class: ActionClass
    proposed_action: str
    params_json: str = "{}"
    risk: str = ""
    evidence: str = ""
    state: ApprovalState
    decided_by: str | None = None
    decision_note: str | None = None
    decided_at: str | None = None
    expires_at: str | None = None
    created_at: str


class Artifact(ApiModel):
    id: str
    run_id: str
    type: str = "file"
    uri: str
    sha256: str = ""
    bytes: int = 0
    created_at: str


class Budget(ApiModel):
    id: str
    scope_type: str
    scope_id: str = ""
    wall_ms_max: int | None = None
    tokens_max: int | None = None
    cost_usd_max: float | None = None
    used_wall_ms: int = 0
    used_tokens: int = 0
    used_cost_usd: float = 0
    updated_at: str


class Pause(ApiModel):
    id: int = 1
    paused: bool
    reason: str = ""
    updated_at: str


class Event(ApiModel):
    id: int
    ts: str
    type: str
    actor: str
    action: str
    target_type: str = ""
    target_id: str = ""
    payload_json: str = "{}"
    trace_id: str = ""


class Heartbeat(ApiModel):
    worker_id: str
    pid: int
    started_at: str
    last_beat_at: str
    current_run_id: str | None = None


class IdempotencyReceipt(ApiModel):
    key: str
    run_id: str
    step_name: str
    result_json: str | None = None
    created_at: str
    completed_at: str | None = None


class StepSpec(ApiModel):
    name: str
    kind: Literal["agent", "effect", "validate"]
    action_class: ActionClass = ActionClass.SANDBOXED_CREATION
    params: dict[str, Any] = Field(default_factory=dict)


class CreateDisciplineRequest(ApiModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    metric_contract: dict[str, Any] | str | None = None


class CreateTaskRequest(ApiModel):
    title: str = Field(min_length=1)
    discipline_id: str | None = None
    project_id: str | None = None
    input: dict[str, Any] | None = None
    acceptance: dict[str, Any] | None = None
    risk: str | None = None
    tools_allowed: list[str] | None = None


class CreateRunRequest(ApiModel):
    harness: HarnessType
    arm: Arm | None = None
    model: str | None = None
    plan: list[StepSpec] | None = None
    budget: BudgetSpec | None = None
    prompt: str | None = None


class PauseRequest(ApiModel):
    paused: bool
    reason: str | None = None


class ApprovalDecisionRequest(ApiModel):
    decision: Literal["approved", "rejected"]
    note: str | None = None
    decided_by: str | None = None


class Session(ApiModel):
    id: str
    source: Literal["bridge", "external"]
    project_dir: str
    provider: str = "claude"
    session_ref: str | None = None
    state: str
    pid: int | None = None
    model: str | None = None
    title: str | None = None
    budget_usd_max: float | None = None
    cost_usd: float | None = None
    kill_requested: bool = False
    last_activity_at: str | None = None
    created_at: str
    updated_at: str


class SessionHookEvalRequest(ApiModel):
    token: str | None = None
    eval_kind: Literal["policy", "steering"] = "policy"
    session_id: str
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    cwd: str


class SessionHookEvalResponse(ApiModel):
    decision: Literal["allow", "deny"]
    approval_id: str | None = None
    action_hash: str
    reason: str | None = None
    additional_context: str | None = Field(default=None, exclude_if=lambda value: value is None)


class SessionIngestRequest(ApiModel):
    session_id: str
    cwd: str
    hook_event_name: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] = Field(default_factory=dict)
    provider: str = "claude"
    session_ref: str | None = None
    state: str = "running"
    pid: int | None = None
    model: str | None = None
    title: str | None = None


class SessionMessageRequest(ApiModel):
    """An operator instruction queued for an active Session Bridge session."""

    message: str = Field(min_length=1, max_length=10_000)
