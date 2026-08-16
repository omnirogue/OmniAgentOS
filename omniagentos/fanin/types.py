"""Fan-in types: parallel candidates collapse to one verified outcome."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FanInMode(StrEnum):
    UNION = "union"
    SELECT_BEST = "select_best"
    SYNTHESIZE = "synthesize"
    ADJUDICATE = "adjudicate"
    INTEGRATE = "integrate"
    REJECT_REPLAN = "reject_replan"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One producer output offered to fan-in."""

    id: str
    content: Any
    score: float | None = None
    confidence: float | None = None
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalizedCandidate:
    """Schema-checked candidate with a stable content hash."""

    id: str
    content: Any
    content_hash: str
    score: float | None = None
    confidence: float | None = None
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "content_hash": self.content_hash,
            "score": self.score,
            "confidence": self.confidence,
            "source": self.source,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class FanInResult:
    """Pure adjudication outcome — never self-approves work."""

    mode: FanInMode
    selected: tuple[NormalizedCandidate, ...]
    rejected: tuple[NormalizedCandidate, ...]
    decision: str
    rationale: str
    evidence: dict[str, Any] = field(default_factory=dict)
    needs_replan: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "selected": [c.to_dict() for c in self.selected],
            "rejected": [c.to_dict() for c in self.rejected],
            "decision": self.decision,
            "rationale": self.rationale,
            "evidence": dict(self.evidence),
            "needs_replan": self.needs_replan,
        }
