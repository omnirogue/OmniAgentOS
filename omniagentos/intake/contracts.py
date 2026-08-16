"""Intake domain types — the clarifying loop's request/response shapes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# The clarifying agent is bounded so it never loops: after this many answered
# rounds it MUST stop asking and emit a spec (enforced in service.clarify_intake,
# not left to the model's discretion).
MAX_CLARIFY_ROUNDS = 2

_PRIORITIES = ("low", "normal", "high", "urgent")


class ClarifyTurn(BaseModel):
    """One prior question the agent asked and the operator's answer."""

    q: str = ""
    a: str = ""


class RefinedSpec(BaseModel):
    """The precise task spec the clarifying agent produces once it has enough."""

    title: str
    description: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    suggested_discipline: str | None = None
    suggested_priority: str = "normal"
    required_expertise: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def normalized(self) -> RefinedSpec:
        title = self.title.strip() or "Untitled task"
        priority = self.suggested_priority.strip().lower()
        if priority not in _PRIORITIES:
            priority = "normal"
        criteria = [c.strip() for c in self.acceptance_criteria if c and c.strip()]
        discipline = (self.suggested_discipline or "").strip() or None
        return RefinedSpec(
            title=title[:2000],
            description=self.description.strip(),
            acceptance_criteria=criteria,
            suggested_discipline=discipline,
            suggested_priority=priority,
            required_expertise=[t.strip() for t in self.required_expertise if t and t.strip()],
            metadata=dict(self.metadata),
        )


class ClarifyResult(BaseModel):
    """Either a short batch of clarifying questions, or a finished spec."""

    mode: Literal["questions", "spec"]
    # 1..3 targeted questions when mode == "questions"; empty when mode == "spec".
    questions: list[str] = Field(default_factory=list)
    # The refined task spec when mode == "spec"; None while still clarifying.
    spec: RefinedSpec | None = None
    # 0-based count of answered rounds already consumed (for the UI/round guard).
    round: int = 0
    # True when the agent stopped asking because the round budget was exhausted.
    forced: bool = False

    def model_dump_public(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        return data
