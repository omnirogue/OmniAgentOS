"""Pydantic models for omniagentos.selfimprove.

Mirrors the style of `omniagentos.contracts` (StrEnum for closed vocabularies,
pydantic BaseModel with a default_factory timestamp) but lives package-local
rather than in contracts.py: these types are not a cross-package frozen
contract (Wave 0 §"implementation packages ... MUST NOT edit [contracts.py]"),
just this package's own request/result shapes.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omniagentos.contracts import utc_now_iso

# Slug-ish identifier constraint shared by SkillMetadata.skill_id/discipline
# (F7): non-empty, length-bounded, no control characters, no newlines, and no
# wikilink delimiters (`[[`/`]]`) — those are the characters an attacker would
# use to inject extra Markdown structure (e.g. a forged `## Notes (human)`
# section) into a rendered vault note via otherwise-plain-text metadata.
_MAX_IDENTIFIER_LENGTH = 200
_FORBIDDEN_IDENTIFIER_SUBSTRINGS = ("[[", "]]")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _validate_identifier(value: str, *, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must be a non-blank string")
    if len(stripped) > _MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{field_name} must be at most {_MAX_IDENTIFIER_LENGTH} characters")
    if _CONTROL_CHAR_RE.search(stripped):
        raise ValueError(f"{field_name} must not contain control characters or newlines")
    for forbidden in _FORBIDDEN_IDENTIFIER_SUBSTRINGS:
        if forbidden in stripped:
            raise ValueError(f"{field_name} must not contain {forbidden!r} (wikilink delimiter)")
    return stripped


class GateStatus(StrEnum):
    """Closed vocabulary for a verification gate's outcome."""

    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"


class VerificationGate(BaseModel):
    """Evidence that a run/workflow did (or did not) pass its verification
    gate — the single fact `capture_skill` / `append_constraint` gate on.

    Deliberately decoupled from any one artifact shape (a Fusion worker's
    `status.json`, a CI result, a hand-run test transcript): callers build
    one of these however is appropriate for their evidence, or use
    `omniagentos.selfimprove.gate.gate_from_status_json` when the evidence is
    a Fusion session's `status.json`.

    Frozen (immutable) so a gate cannot be mutated between the write
    boundary's status check and the actual write (TOCTOU hardening). The HARD
    RULE is enforced at every write boundary via `gate.status is
    GateStatus.PASSED` directly — never via `.passed`, which is a plain
    `@property` and so is trivially overridable by a subclass; `.passed`
    remains only as a read-only convenience for callers that are not the
    write-boundary enforcement itself.
    """

    model_config = ConfigDict(frozen=True)

    status: GateStatus
    source_run_id: str | None = None
    checked_at: str = Field(default_factory=utc_now_iso)
    evidence: str | None = None  # short human-readable evidence summary

    @property
    def passed(self) -> bool:
        return self.status is GateStatus.PASSED


class CapabilityLearning(BaseModel):
    """One atomic capability emitted alongside a distilled skill."""

    statement: str
    domains: list[str]
    kind: str


class SkillMetadata(BaseModel):
    """Author-supplied description of the reusable workflow to capture.

    This is the WHAT (what the skill is); `VerificationGate` is the WHETHER
    (whether capturing it is currently allowed). `skill_id` should be a
    short, stable, filesystem-safe slug — it becomes both the vault note id
    and the `SKILL.md` folder name.
    """

    skill_id: str
    title: str
    summary: str
    input_format: str
    steps: list[str]
    output_structure: str
    validation_rules: list[str]
    discipline: str | None = None
    tags: list[str] = Field(default_factory=list)
    # The model used to produce the learning, when known.  Kept separately
    # from the human-facing summary so the curator can route model swaps to
    # human review rather than auto-approving them.
    model: str | None = None
    # Plan-07: the distiller may emit multiple capabilities, but each item is
    # persisted independently. Scope is deliberately absent: capture defaults to
    # company and estate promotion is a later, separately gated operation.
    capabilities: list[CapabilityLearning] = Field(default_factory=list)
    company_id: str | None = None
    brand: str | None = None

    @field_validator("skill_id")
    @classmethod
    def _validate_skill_id(cls, value: str) -> str:
        return _validate_identifier(value, field_name="skill_id")

    @field_validator("discipline")
    @classmethod
    def _validate_discipline(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, field_name="discipline")


class SkillCaptureResult(BaseModel):
    """Return value of `capture_skill` / `capture_skill_from_run_dir`."""

    skill_id: str
    note_path: str
    skill_md_path: str | None = None
    capability_fact_ids: list[int] = Field(default_factory=list)
    capability_note_paths: list[str] = Field(default_factory=list)


class ConstraintEntry(BaseModel):
    """One appended line item in a project's CONSTRAINTS.md."""

    project: str
    rule: str
    source_run_id: str | None = None
    created: str = Field(default_factory=utc_now_iso)
