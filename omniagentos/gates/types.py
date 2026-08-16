"""Shared types for the G0–G10 policy gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class GateId(StrEnum):
    G0 = "G0"
    G1 = "G1"
    G2 = "G2"
    G3 = "G3"
    G4 = "G4"
    G5 = "G5"
    G6 = "G6"
    G7 = "G7"
    G8 = "G8"
    G9 = "G9"
    G10 = "G10"


@dataclass(frozen=True, slots=True)
class GateDecision:
    gate_id: GateId
    decision: str
    evidence: dict[str, Any] = field(default_factory=dict)
    next_state: str = ""
    policy_version: str = "v1"
