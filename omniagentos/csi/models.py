"""CSI domain models — evidence packet + planner plan schema."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class EvidenceRef(BaseModel):
    kind: str  # trace | metric | run | skill | memory
    id: str
    summary: str = ""


class EvidencePacket(BaseModel):
    routine: str
    window_days: int
    codebase_sha: str = ""
    generated_at: str = ""
    traces: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    control_metrics: dict[str, Any] = Field(default_factory=dict)
    open_incidents: list[dict[str, Any]] = Field(default_factory=list)
    active_worktrees: list[str] = Field(default_factory=list)
    frozen_surfaces: list[str] = Field(default_factory=list)
    observe_only_areas: list[str] = Field(default_factory=list)
    existing_memories: list[dict[str, Any]] = Field(default_factory=list)
    existing_skills: list[dict[str, Any]] = Field(default_factory=list)
    repeat_failures: list[dict[str, Any]] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    thin_evidence: bool = True


class PlannerProblem(BaseModel):
    rank: int = 1
    title: str
    evidence_refs: list[str] = Field(default_factory=list)
    frequency: str = ""
    impact: str = ""
    root_cause: str = ""
    confidence: float = 0.0


class PlannerProposal(BaseModel):
    addresses_problem_rank: int = 1
    change: str
    affected_paths: list[str] = Field(default_factory=list)
    baseline: str = ""
    target: str = ""
    measured_by: str = ""
    minimum_effect_size: str = ""
    tests_required: list[str] = Field(default_factory=list)
    migration_required: bool = False
    risks: list[str] = Field(default_factory=list)
    rollback: str = ""
    estimated_scope: Literal["small", "medium", "large"] = "small"
    confidence: float = 0.0


class PlannerPlan(BaseModel):
    routine: str
    verdict: Literal["propose", "no_change"] = "no_change"
    no_change_reason: str = ""
    problems: list[PlannerProblem] = Field(default_factory=list)
    proposals: list[PlannerProposal] = Field(default_factory=list)
    unsupported_hypotheses: list[str] = Field(default_factory=list)
    human_review_items: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    planner: str = ""
    lineage: str = ""
    execution_id: str = ""
    provenance_verified: bool = False


class SynthesisResult(BaseModel):
    verdict: Literal["propose", "no_change", "insufficient_panel"] = "no_change"
    reason: str = ""
    accepted_proposals: list[PlannerProposal] = Field(default_factory=list)
    rejected_items: list[dict[str, Any]] = Field(default_factory=list)
    human_review_items: list[str] = Field(default_factory=list)
    panel_size: int = 0


class ConflictForecast(BaseModel):
    safe_to_implement: bool = True
    reasons: list[str] = Field(default_factory=list)
    active_user_worktrees: list[str] = Field(default_factory=list)
    overlapping_paths: list[str] = Field(default_factory=list)


class CsiRunResult(BaseModel):
    run_id: str
    routine_id: str
    status: str
    verdict: str
    no_change_reason: str = ""
    synthesis: SynthesisResult | None = None
    conflict: ConflictForecast | None = None
    improvement_id: str | None = None
    wall_clock_s: float = 0.0
    error: str | None = None


__all__ = [
    "ConflictForecast",
    "CsiRunResult",
    "EvidencePacket",
    "EvidenceRef",
    "PlannerPlan",
    "PlannerProblem",
    "PlannerProposal",
    "SynthesisResult",
]
