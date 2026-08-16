"""Schemas for artifacts, memory, metacognition, strategies, reflection."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from omniagentos.contracts import new_id, utc_now_iso

ControlAction = Literal[
    "continue", "widen", "prune", "replan", "escalate", "stop", "checkpoint", "switch"
]
MemoryType = Literal["fact", "lesson", "procedure", "strategy", "warning", "benchmark"]
PromotionStatus = Literal["pending", "shadow", "promoted", "rejected", "superseded"]
QualityTrend = Literal["improving", "flat", "degrading"]
UncertaintyType = Literal["factual", "implementation", "evaluation", "environmental", "unknown"]


class ArtifactEnvelope(BaseModel):
    id: str = Field(default_factory=lambda: new_id("art"))
    artifact_type: str
    company_id: str = "default"
    project_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    producer_agent_id: str | None = None
    content_uri: str = ""
    content_hash: str = ""
    format: str = "json"
    schema_version: int = 1
    base_repo: str | None = None
    base_sha: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    quality: dict[str, Any] = Field(default_factory=dict)
    retention_class: str = "project"
    access_scope: str = "task"
    supersedes: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class CheckpointPacket(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ckp"))
    run_id: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    company_id: str = "default"
    project_id: str | None = None
    state: dict[str, Any] = Field(default_factory=dict)
    completed_criteria: list[str] = Field(default_factory=list)
    pending_criteria: list[str] = Field(default_factory=list)
    side_effects: list[dict[str, Any]] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    context_digest: str | None = None
    safe_to_resume: bool = True
    replay_risk: str = "none"
    created_at: str = Field(default_factory=utc_now_iso)


class MemoryRecord(BaseModel):
    id: str = Field(default_factory=lambda: new_id("mem"))
    type: MemoryType = "lesson"
    statement: str
    applicability: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    sample_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    promotion_status: PromotionStatus = "pending"
    valid_from: str | None = None
    expires_at: str | None = None
    invalidated_by: dict[str, Any] = Field(default_factory=dict)
    supersedes: list[str] = Field(default_factory=list)
    owner: str = "memory_curator"
    company_id: str = "default"
    project_id: str | None = None
    domain: str | None = None
    helpfulness_score: float = 0.0
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class MetacognitiveState(BaseModel):
    objective_progress: float = 0.0
    acceptance_criteria_passed: str = "0/0"
    # False means this snapshot has no honest, quantitative progress signal and
    # must remain neutral in stall/replan accounting.
    progress_measured: bool = True
    strategy_id: str | None = None
    strategy_age_iterations: int = 0
    new_evidence_delta: float = 0.0
    quality_trend: QualityTrend = "flat"
    confidence: float = 0.5
    uncertainty_type: UncertaintyType = "unknown"
    stall_count: int = 0
    repetition_score: float = 0.0
    context_pressure: float = 0.0
    cost_used: float = 0.0
    time_used: float = 0.0
    fanout_active: int = 0
    verifier_capacity_reserved: int = 0
    next_control_action: ControlAction = "continue"
    reason_codes: list[str] = Field(default_factory=list)


class ContextPacket(BaseModel):
    task_id: str | None = None
    run_id: str | None = None
    block: str = ""
    artifact_ids: list[str] = Field(default_factory=list)
    memory_ids: list[str] = Field(default_factory=list)
    skill_slugs: list[str] = Field(default_factory=list)
    omissions: list[str] = Field(default_factory=list)
    estimated_tokens: int = 0
    budget_tokens: int = 0


class StrategyDef(BaseModel):
    id: str
    title: str
    description: str = ""
    topology: str = "solo"
    effort: str = "medium"
    max_fanout: int = 1
    requires_verifier: bool = False
    eligible_domains: list[str] = Field(default_factory=lambda: ["coding"])
    performance: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ReflectionReport(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rfl"))
    run_id: str | None = None
    task_id: str | None = None
    outcome: str
    taxonomy: str = "unknown"
    report: dict[str, Any] = Field(default_factory=dict)
    candidate_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)


class SkillVersion(BaseModel):
    id: str = Field(default_factory=lambda: new_id("skv"))
    skill_slug: str
    version: str
    body_md: str
    status: str = "shadow"
    source_memory_ids: list[str] = Field(default_factory=list)
    fixtures: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


# Built-in strategies (Phase 5)
BUILTIN_STRATEGIES: tuple[StrategyDef, ...] = (
    StrategyDef(
        id="solo_executor",
        title="Single executor",
        description="One worker produces; optional verify later.",
        topology="solo",
        effort="medium",
        max_fanout=1,
    ),
    StrategyDef(
        id="generator_critic",
        title="Generator + critic",
        description="Worker produces; independent verifier must pass.",
        topology="generator_critic",
        effort="high",
        max_fanout=1,
        requires_verifier=True,
    ),
    StrategyDef(
        id="hypothesis_portfolio",
        title="Hypothesis portfolio",
        description="Fan out alternate approaches for novel problems.",
        topology="portfolio",
        effort="high",
        max_fanout=3,
        requires_verifier=True,
    ),
    StrategyDef(
        id="repair_loop",
        title="Bounded repair loop",
        description="After failure, replan once with reflection then stop.",
        topology="repair",
        effort="medium",
        max_fanout=1,
    ),
)
