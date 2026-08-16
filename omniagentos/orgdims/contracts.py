"""Shared contracts for org dimensions + classification."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.orgdims.taxonomy import TAXONOMY_VERSION

ObjectType = Literal["board_task", "skill", "agent", "loop"]
ClassStatus = Literal["suggested", "provisional", "applied", "locked", "rejected"]


class OrganizationContext(BaseModel):
    company_id: str | None = None
    company_slug: str | None = None
    product_id: str | None = None
    product_slug: str | None = None
    initiative_id: str | None = None
    goal_ids: list[str] = Field(default_factory=list)
    epic_id: str | None = None


class Classification(BaseModel):
    primary_workstream: str | None = None
    domains: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    lifecycle: str | None = None
    priority: str | None = None
    risk_class: str | None = None


class Provenance(BaseModel):
    source: str = "ai_classification"
    confidence: float = 0.0
    evidence_refs: list[str] = Field(default_factory=list)
    taxonomy_version: int = TAXONOMY_VERSION
    rationale: str = ""
    classifier_agent: str | None = "grok-orchestrator"


class DimensionBundle(BaseModel):
    """Full multidimensional envelope attached to a board card / skill / etc."""

    organization_context: OrganizationContext = Field(default_factory=OrganizationContext)
    classification: Classification = Field(default_factory=Classification)
    execution_links: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance = Field(default_factory=Provenance)
    locked_fields: list[str] = Field(default_factory=list)


class ClassificationResult(BaseModel):
    object_type: ObjectType
    object_id: str
    bundle: DimensionBundle
    status: ClassStatus = "applied"
    field_confidences: dict[str, float] = Field(default_factory=dict)
    auto_applied: list[str] = Field(default_factory=list)
    provisional: list[str] = Field(default_factory=list)
    needs_review: list[str] = Field(default_factory=list)


class GrokAgentProfile(BaseModel):
    id: str = Field(default_factory=lambda: new_id("oap"))
    slug: str
    name: str
    lineage: str = "cli-grok"
    model: str = "grok-4.5"
    role: str
    metacog_primary: bool = True
    expertise: list[str] = Field(default_factory=list)
    workstreams: list[str] = Field(default_factory=list)
    risk_ceiling: str = "bounded_external"
    charter: str = ""
    enabled: bool = True
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


# Primary Grok metacognition / self-learning control agents.
OMNIAGENTOS_METACOG_AGENTS: tuple[GrokAgentProfile, ...] = (
    GrokAgentProfile(
        slug="grok-orchestrator",
        name="Grok Orchestrator",
        role="orchestrator",
        expertise=[
            "metacognition",
            "orchestration",
            "strategy",
            "routing",
            "classification",
            "portfolio",
        ],
        workstreams=["engineering", "executive-strategy", "operations"],
        risk_ceiling="bounded_external",
        charter=(
            "Primary metacognitive controller for OmniAgentOS. Owns progress "
            "monitoring, strategy selection, fan-out/stop gates, and dimension "
            "classification for routing. Does not execute domain side effects; "
            "delegates to workers and verifiers."
        ),
    ),
    GrokAgentProfile(
        slug="grok-self-repair",
        name="Grok Self-Repair",
        role="self_repair",
        expertise=["repair", "incident", "retry", "rollback", "reliability"],
        workstreams=["engineering", "operations", "infrastructure"],
        risk_ceiling="reversible_internal",
        charter=(
            "Detects stalls, failures, and regressions; proposes bounded repair "
            "loops, rollbacks, and strategy switches with evidence. Never widens "
            "scope without verifier capacity."
        ),
    ),
    GrokAgentProfile(
        slug="grok-self-learn",
        name="Grok Self-Learn",
        role="self_learn",
        expertise=["reflection", "memory", "skills", "learning", "evaluation"],
        workstreams=["research", "engineering", "data-analytics"],
        risk_ceiling="read_only",
        charter=(
            "Turns completed/failed runs into memory candidates and skill versions. "
            "Owns reflection → promotion → skill synthesis with evidence gates."
        ),
    ),
    GrokAgentProfile(
        slug="grok-memory-curator",
        name="Grok Memory Curator",
        role="memory_curator",
        expertise=["memory", "invalidation", "provenance", "retrieval"],
        workstreams=["research", "engineering"],
        risk_ceiling="read_only",
        charter=(
            "Promotes, narrows, supersedes, or invalidates typed memory. Guards "
            "against stale/cross-company leakage."
        ),
    ),
    GrokAgentProfile(
        slug="grok-verifier",
        name="Grok Verifier",
        role="verifier",
        expertise=["verification", "acceptance", "qa", "adjudication"],
        workstreams=["engineering", "qa", "product"],
        risk_ceiling="read_only",
        charter=(
            "Independent evidence-backed verification of acceptance criteria. "
            "Executor narrative is non-evidence."
        ),
    ),
    GrokAgentProfile(
        slug="grok-strategy-selector",
        name="Grok Strategy Selector",
        role="strategy_selector",
        expertise=["strategy", "topology", "model-routing", "cost"],
        workstreams=["engineering", "executive-strategy"],
        risk_ceiling="read_only",
        charter=(
            "Chooses loop topology, model effort, and fallback ladder from "
            "task dimensions and historical performance."
        ),
    ),
)
