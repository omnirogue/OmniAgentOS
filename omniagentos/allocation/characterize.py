"""Pure heuristics for describing the shape of a requested task."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_CLASS_ORDER = ("S", "P", "E", "V", "X", "C", "N")


def _unit(value: Any, default: float = 0.0) -> float:
    """Convert common signal spellings to a bounded scalar."""
    if value is None:
        return default
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _amount(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class TaskCharacterization:
    """Normalized task features used by allocation policy.

    ``D`` is decomposability, ``I`` independence, ``S`` sequentiality, ``U``
    uncertainty, ``V`` verifiability, ``G`` goal clarity, ``C`` criticality,
    ``M`` multi-specialty need, ``R`` risk, and ``K`` knowledge intensity.
    ``W`` and ``P`` retain unbounded work-volume and pressure signals.
    """

    D: float
    I: float  # noqa: E741 -- the model's own letter (see class docstring), not an index
    S: float
    U: float
    V: float
    G: float
    C: float
    M: float
    R: float
    K: float
    W: float
    P: float
    confidence: float
    task_class: list[str]

    def to_dict(self) -> dict[str, float | list[str]]:
        """A stable, JSON-ready description suitable for signing or recording."""
        return {
            "D": self.D,
            "I": self.I,
            "S": self.S,
            "U": self.U,
            "V": self.V,
            "G": self.G,
            "C": self.C,
            "M": self.M,
            "R": self.R,
            "K": self.K,
            "W": self.W,
            "P": self.P,
            "confidence": self.confidence,
            "task_class": list(self.task_class),
        }


def characterize(signals: dict[str, Any]) -> TaskCharacterization:
    """Characterize ``signals`` without model calls, time, or mutable state.

    Callers may supply normalized feature letters directly; conventional signal
    names are then used as deterministic fallbacks.  This makes the policy easy
    to tune upstream while retaining useful behavior for a minimal intake form.
    """
    partitions = _amount(signals.get("partition_count"))
    units = _amount(signals.get("independent_units"))
    sequential = _unit(signals.get("sequential"))
    uncertainty = _unit(signals.get("uncertainty"))
    risk = _unit(signals.get("risk"))
    critical = _unit(signals.get("critical"))
    verifiable = _unit(signals.get("verifiable"))
    specialty = _unit(signals.get("multi_specialty"))
    knowledge = _unit(signals.get("knowledge_heavy"))
    exploratory = _unit(signals.get("exploratory"))

    decomposable = max(
        _unit(signals.get("D")),
        _unit(signals.get("has_partitions")),
        min(1.0, max(partitions - 1.0, units - 1.0) / 3.0),
    )
    independence = max(
        _unit(signals.get("I")),
        _unit(signals.get("has_partitions")),
        min(1.0, units / 4.0),
    )
    s = max(_unit(signals.get("S")), sequential)
    u = max(_unit(signals.get("U")), uncertainty, exploratory)
    v = max(_unit(signals.get("V")), verifiable)
    g = max(_unit(signals.get("G")), 1.0 - u)
    c = max(_unit(signals.get("C")), critical)
    m = max(_unit(signals.get("M")), specialty)
    r = max(_unit(signals.get("R")), risk, critical * 0.75)
    k = max(_unit(signals.get("K")), knowledge)
    w = _amount(signals.get("work_volume"))
    p = _amount(signals.get("urgency"))

    observed = sum(
        key in signals
        for key in (
            "has_partitions",
            "partition_count",
            "sequential",
            "uncertainty",
            "risk",
            "verifiable",
            "critical",
            "multi_specialty",
            "knowledge_heavy",
            "work_volume",
            "urgency",
            "independent_units",
            "exploratory",
        )
    )
    confidence = min(1.0, 0.25 + observed / 16.0)
    classes = [
        letter
        for letter, enabled in (
            ("S", s >= 0.6),
            ("P", decomposable >= 0.6 and independence >= 0.5),
            ("E", exploratory >= 0.6 or u >= 0.75),
            ("V", v >= 0.6),
            ("X", r >= 0.6 or c >= 0.75),
            ("C", m >= 0.6),
            ("N", k >= 0.6),
        )
        if enabled
    ]
    return TaskCharacterization(
        D=decomposable,
        I=independence,
        S=s,
        U=u,
        V=v,
        G=g,
        C=c,
        M=m,
        R=r,
        K=k,
        W=w,
        P=p,
        confidence=confidence,
        task_class=[letter for letter in _CLASS_ORDER if letter in classes],
    )
