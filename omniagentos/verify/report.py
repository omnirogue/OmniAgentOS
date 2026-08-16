"""Verification report model — pure judgment record (no I/O, no DB).

Two verdict vocabularies exist in this repo and are **different dimensions**:

- Scheduler / reviewer infrastructure (``SwarmReviewOutcome`` in
  ``omniagentos.swarm.scheduler``): ``confirm | deny | error``, where ``error``
  means reviewer **infrastructure** failure — explicitly NOT a deny and never an
  auto-confirm. That outcome is the **absence of a report**, never a report row.
- Judgment tri-state (this module, aligned with ``vault/prompts/roles/reviewer.md``
  rule 6): ``pass | fail | insufficient_evidence``.

``VerificationReport.verdict`` carries **only** the judgment tri-state. There is
no ``error`` member and no way to construct a report expressing one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "BLOCKING_SEVERITIES",
    "Finding",
    "Severity",
    "VerificationError",
    "VerificationReport",
    "Verdict",
]


class VerificationError(ValueError):
    """Invalid verification report or finding payload."""


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        """Lower rank == more severe (critical=0 … low=3)."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
}


class Verdict(StrEnum):
    """Judgment tri-state only.

    No ``error`` member — reviewer infrastructure failure is the ABSENCE of a
    report (scheduler vocabulary: confirm | deny | error). Do not collapse the
    two dimensions.
    """

    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


BLOCKING_SEVERITIES: tuple[Severity, ...] = (Severity.CRITICAL, Severity.HIGH)


def _coerce_severity(value: Severity | str) -> Severity:
    if isinstance(value, Severity):
        return value
    try:
        return Severity(str(value))
    except ValueError as exc:
        raise VerificationError(f"unknown severity: {value!r}") from exc


def _coerce_verdict(value: Verdict | str) -> Verdict:
    if isinstance(value, Verdict):
        return value
    raw = str(value)
    try:
        return Verdict(raw)
    except ValueError as exc:
        raise VerificationError(
            f"verdict must be pass|fail|insufficient_evidence, got {raw!r}"
        ) from exc


@dataclass(frozen=True, slots=True)
class Finding:
    """One concrete failure scenario against an artifact."""

    severity: Severity
    file: str
    line: int | None
    summary: str
    failure_scenario: str
    id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "severity", _coerce_severity(self.severity))
        if not self.summary or not str(self.summary).strip():
            raise VerificationError("finding summary must be non-empty")
        if not self.failure_scenario or not str(self.failure_scenario).strip():
            raise VerificationError(
                "finding failure_scenario must be non-empty "
                "(a finding without the failure scenario that makes it real is not a finding)"
            )

    def key(self) -> str:
        """Stable name a waiver must use: explicit id, else a derived composite."""
        if self.id is not None and str(self.id).strip():
            return str(self.id)
        return f"{self.severity}:{self.file}:{self.line}:{self.summary}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "file": self.file,
            "line": self.line,
            "summary": self.summary,
            "failure_scenario": self.failure_scenario,
            "id": self.id,
        }


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Judgment of one artifact revision — pure data, no side effects.

    ``reviewer_lineage`` / ``implementer_lineage`` are carried **verbatim** for
    cross-lineage independence checks (W2.2); never normalize, lowercase,
    default, or swap them.
    """

    artifact_hash: str
    verdict: Verdict
    findings: tuple[Finding, ...] = ()
    verifier: str = ""
    reviewer_lineage: str = ""
    implementer_lineage: str = ""
    evidence: tuple[str, ...] = ()
    created_at: str | None = None

    def __post_init__(self) -> None:
        if not self.artifact_hash or not str(self.artifact_hash).strip():
            raise VerificationError(
                "artifact_hash must be non-empty "
                "(a report cannot exist without the artifact hash it judged)"
            )
        object.__setattr__(self, "verdict", _coerce_verdict(self.verdict))
        # Most severe first (ascending rank); stable within a severity band.
        sorted_findings = tuple(sorted(self.findings, key=lambda f: f.severity.rank))
        object.__setattr__(self, "findings", sorted_findings)

    def is_stale_for(self, current_hash: str) -> bool:
        """True when ``current_hash`` does not match this report's artifact.

        Empty/blank ``current_hash`` is stale (fail-closed), never a match.
        """
        if not current_hash or not str(current_hash).strip():
            return True
        return current_hash != self.artifact_hash

    def satisfies(self, current_hash: str) -> bool:
        """PASS against the same artifact hash — nothing else."""
        return self.verdict is Verdict.PASS and not self.is_stale_for(current_hash)

    def status_for(self, current_hash: str) -> str:
        """Lifecycle status of this report vs ``current_hash``.

        Returns one of ``satisfied | stale | failed | insufficient_evidence``.

        - ``insufficient_evidence`` always, for every hash (matching or not).
        - Hash mismatch (including blank) → ``stale`` for PASS and FAIL alike —
          the artifact was judged at a different revision, not judged bad now.
        - Matching PASS → ``satisfied``; matching FAIL → ``failed``.
        """
        if self.verdict is Verdict.INSUFFICIENT_EVIDENCE:
            return "insufficient_evidence"
        if self.is_stale_for(current_hash):
            return "stale"
        if self.verdict is Verdict.PASS:
            return "satisfied"
        return "failed"

    def blocking_findings(self, waivers: Sequence[str] = ()) -> tuple[Finding, ...]:
        """Critical/high findings whose ``key()`` is not named exactly in ``waivers``."""
        waived = set(waivers)
        return tuple(
            f
            for f in self.findings
            if f.severity in BLOCKING_SEVERITIES and f.key() not in waived
        )

    def has_open_blocking_findings(self, waivers: Sequence[str] = ()) -> bool:
        return bool(self.blocking_findings(waivers))

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_hash": self.artifact_hash,
            "verdict": self.verdict.value,
            "findings": [f.to_dict() for f in self.findings],
            "verifier": self.verifier,
            "reviewer_lineage": self.reviewer_lineage,
            "implementer_lineage": self.implementer_lineage,
            "evidence": list(self.evidence),
            "created_at": self.created_at,
        }
