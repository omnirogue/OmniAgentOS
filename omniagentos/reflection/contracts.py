"""Data structures and JSON schemas for the nightly self-learning reflection loop.

Includes the schema of ImprovementProposal exactly as defined in the plan,
as well as the ReflectionEvidence and other pipeline run records.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class FileTarget(BaseModel):
    """File-key configuration target for an improvement."""

    file: str
    key: str


class DocTarget(BaseModel):
    """Document documentation target for an improvement."""

    doc: str


class ImprovementProposal(BaseModel):
    """An LLM-proposed, structured improvement proposal to config or documentation."""

    id: str
    kind: Literal[
        "model_config",
        "formation",
        "router_weight",
        "effort_override",
        "category_pin",
        "brief_template",
        "lesson",
        "skill",
        "rollback",
    ]
    target: FileTarget | DocTarget
    current: str
    proposed: str
    rationale: str
    evidence_refs: list[str] = Field(default_factory=list)
    predicted_impact: str
    risk_class: str
    promotion_status: Literal["pending", "shadow", "promoted", "rejected", "superseded"] = "pending"


class SourceDigest(BaseModel):
    """A generic digested summary of a source."""

    source_name: str
    bytes_read: int
    tokens_estimated: int
    caps_hit: list[str] = Field(default_factory=list)
    summary_or_sample: str


class ReflectionEvidence(BaseModel):
    """The complete, typed, schema-validated nightly evidence packet."""

    date: str  # YYYY-MM-DD
    started_at: str
    finished_at: str
    # Database metrics
    runs: list[dict[str, Any]] = Field(default_factory=list)
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    sessions: list[dict[str, Any]] = Field(default_factory=list)
    formation_selections: list[dict[str, Any]] = Field(default_factory=list)
    metacog_memory_candidates: list[dict[str, Any]] = Field(default_factory=list)
    # File-based ledgers
    session_logs: list[dict[str, Any]] = Field(default_factory=list)
    router_shadow_logs: list[dict[str, Any]] = Field(default_factory=list)
    openhands_gate_denials: list[dict[str, Any]] = Field(default_factory=list)
    runs_ledger_attempts: list[dict[str, Any]] = Field(default_factory=list)
    workbook_end_states: list[dict[str, Any]] = Field(default_factory=list)
    # Harness transcripts
    harness_transcripts: dict[str, list[SourceDigest]] = Field(default_factory=dict)
    # Compounding historical learnings (last 28 days of .md lessons and briefings)
    compiled_learnings: list[dict[str, Any]] = Field(default_factory=list)
    # Automatically classified failure tags
    failure_tags: list[dict[str, Any]] = Field(default_factory=list)
    # Cap-enforcement tracking
    bytes_read: int = 0
    caps_hit: list[str] = Field(default_factory=list)
