"""Pydantic request schemas for the frozen skills-library HTTP surface."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SkillCreate(BaseModel):
    slug: str = Field(min_length=1)
    category: str = Field(min_length=1)
    subcategory: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str = ""
    preferred_method: str | None = None
    fallback_method: str | None = None


class SkillEdit(BaseModel):
    content: str
    preferred_method: str | None = None
    fallback_method: str | None = None
    change_reason: str = Field(min_length=1)


class UpdateProposalCreate(BaseModel):
    proposed_content: str | None = None
    proposed_diff: str | None = None
    evidence_files: list[str] | None = None
    linked_execution: str | None = None
    risk: Literal["low", "model", "major"] = "low"
    created_by: str = "api"

    @model_validator(mode="after")
    def require_change(self) -> UpdateProposalCreate:
        if self.proposed_content is None and self.proposed_diff is None:
            raise ValueError("proposed_content or proposed_diff is required")
        return self


class ProposalDecision(BaseModel):
    approve: bool
    note: str | None = None
    decided_by: str = Field(min_length=1)


class PreferredMethod(BaseModel):
    method: str = Field(min_length=1)


class QuarantineRelease(BaseModel):
    """Operator reversal of a machine quarantine (U-16).

    ``released_by`` is required and un-defaulted: a release is an operator
    decision and must name the operator who made it.
    """

    released_by: str = Field(min_length=1)
    note: str | None = None


__all__ = [
    "PreferredMethod",
    "ProposalDecision",
    "QuarantineRelease",
    "SkillCreate",
    "SkillEdit",
    "UpdateProposalCreate",
]
