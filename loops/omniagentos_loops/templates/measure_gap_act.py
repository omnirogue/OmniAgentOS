"""Standalone dry-run driver for measure -> evaluate -> diagnose -> receipt.

This module is not a registered :class:`LoopTemplate`; the routine cadence is
the clock that invokes it. Each cycle records exactly one honest reading, and
cap, pacing, and refusal outcomes are returned as receipts rather than written
to goal state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from omniagentos.contracts import utc_now_iso

_COMPARATORS = frozenset({">=", "<=", "=="})
_EQ_TOLERANCE = 1e-9


@dataclass(frozen=True)
class Measurement:
    """A measured value, optionally carrying an instrument-fault diagnosis."""

    value: float | None
    fault: str | None = None


class MeasureFn(Protocol):
    def __call__(self) -> float | None | Measurement: ...  # pragma: no cover


class RecordFn(Protocol):
    def __call__(
        self, *, cycle: int, value: float | None, met: bool, captured_at: str
    ) -> dict[str, Any]: ...  # pragma: no cover


class EvaluateFn(Protocol):
    def __call__(self, *, current_cycle: int) -> bool: ...  # pragma: no cover


class PaceFn(Protocol):
    def __call__(
        self, *, cycle: int, attempted_at: datetime
    ) -> PacingReceipt | None: ...  # pragma: no cover


def meets_setpoint(value: float | None, comparator: str, target: float) -> bool:
    """Return whether a finite value meets the setpoint; absence never does."""
    if value is None:
        return False
    if comparator not in _COMPARATORS:
        raise ValueError(f"unsupported comparator: {comparator!r}")
    if comparator == ">=":
        return value >= target
    if comparator == "<=":
        return value <= target
    return math.isclose(value, target, rel_tol=0, abs_tol=_EQ_TOLERANCE)


@dataclass(frozen=True)
class CycleReceipt:
    """What one recorded tick reports; it is never written to goal state."""

    goal_id: str
    cycle: int
    value: float | None
    met: bool
    comparator: str
    target: float
    sustain_periods: int
    sustained: bool
    graduation_verdict: bool
    detail: str
    captured_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "cycle": self.cycle,
            "value": self.value,
            "met": self.met,
            "comparator": self.comparator,
            "target": self.target,
            "sustain_periods": self.sustain_periods,
            "sustained": self.sustained,
            # Pacing makes every sustained member a real stored period. This
            # remains a dry-run synonym: no status/graduated_at write occurs.
            "graduation_verdict_dry_run_only": self.graduation_verdict,
            "detail": self.detail,
            "captured_at": self.captured_at,
        }


@dataclass(frozen=True)
class EscalationReceipt:
    goal_id: str
    cycles_run: int
    max_cycles: int
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "cycles_run": self.cycles_run,
            "max_cycles": self.max_cycles,
            "escalated": True,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PacingReceipt:
    goal_id: str
    cycle: int
    previous_captured_at: str
    attempted_at: str
    window_seconds: float
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "pacing_refused": True}


@dataclass(frozen=True)
class RefusalReceipt:
    goal_id: str
    reason: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "refused": True}


def _canonical_timestamp(now: datetime | None) -> str:
    if now is None:
        return utc_now_iso()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _measurement(measure: MeasureFn) -> Measurement:
    try:
        measured = measure()
        result = measured if isinstance(measured, Measurement) else Measurement(measured)
        if result.value is not None and not math.isfinite(result.value):
            return Measurement(None, f"non-finite value {result.value!r}")
        return result
    except Exception as exc:  # instrument drift must leave evidence
        return Measurement(None, f"{type(exc).__name__}: {exc}")


def run_cycle(
    *,
    goal_id: str,
    cycle: int,
    comparator: str,
    target: float,
    sustain_periods: int,
    measure: MeasureFn,
    record: RecordFn,
    evaluate: EvaluateFn,
    now: datetime | None = None,
) -> CycleReceipt:
    """Run one cycle and build the receipt from the row actually stored."""
    measurement = _measurement(measure)
    met = meets_setpoint(measurement.value, comparator, target)
    intended_at = _canonical_timestamp(now)
    stored = record(cycle=cycle, value=measurement.value, met=met, captured_at=intended_at)
    sustained = evaluate(current_cycle=cycle)
    stored_value = stored.get("value")
    value = float(stored_value) if stored_value is not None else None
    stored_met = bool(stored.get("met", False))
    captured_at = str(stored.get("captured_at", intended_at))
    if measurement.fault:
        detail = f"cycle {cycle}: instrument fault ({measurement.fault}); recorded no reading"
    elif value is None:
        detail = f"cycle {cycle}: no reading (metric source returned no value)"
    else:
        detail = (
            f"cycle {cycle}: value={value!r} {comparator} {target!r} -> met={stored_met}, "
            f"sustained={sustained} ({sustain_periods} periods)"
        )
    return CycleReceipt(
        goal_id=goal_id,
        cycle=int(stored.get("cycle", cycle)),
        value=value,
        met=stored_met,
        comparator=comparator,
        target=target,
        sustain_periods=sustain_periods,
        sustained=sustained,
        graduation_verdict=sustained,
        detail=detail,
        captured_at=captured_at,
    )


@dataclass
class DryRunController:
    """Drive only the goal's remaining lifetime cycles, stopping on refusal."""

    goal_id: str
    comparator: str
    target: float
    sustain_periods: int
    max_cycles: int
    measure: MeasureFn
    record: RecordFn
    evaluate: EvaluateFn
    start_cycle: int = 0
    effort_cap: int | None = None
    pace: PaceFn | None = None
    now: datetime | None = None
    receipts: list[CycleReceipt] = field(default_factory=list)
    escalation: EscalationReceipt | None = None
    pacing: PacingReceipt | None = None

    def __post_init__(self) -> None:
        if self.max_cycles < 0:
            raise ValueError("max_cycles must be >= 0")
        if self.sustain_periods < 1:
            raise ValueError("sustain_periods must be >= 1")
        if self.effort_cap is None:
            self.effort_cap = self.max_cycles

    def _escalate(self, *, already_exhausted: bool) -> None:
        cap = int(self.effort_cap or 0)
        detail = (
            f"goal {self.goal_id}: effort cap {cap} was already exhausted before this invocation"
            if already_exhausted
            else f"goal {self.goal_id}: effort cap exhausted without a sustained streak of "
            f"{self.sustain_periods} periods — escalating, not cycling further"
        )
        self.escalation = EscalationReceipt(
            goal_id=self.goal_id,
            cycles_run=len(self.receipts),
            max_cycles=cap,
            detail=detail,
        )

    def run(self) -> list[CycleReceipt]:
        """Run until graduation, pacing refusal, or the lifetime cap."""
        if self.max_cycles == 0:
            self._escalate(already_exhausted=True)
            return self.receipts
        for offset in range(self.max_cycles):
            cycle = self.start_cycle + offset
            attempted_at = self.now or datetime.now(UTC)
            if self.pace is not None:
                self.pacing = self.pace(cycle=cycle, attempted_at=attempted_at)
                if self.pacing is not None:
                    return self.receipts
            receipt = run_cycle(
                goal_id=self.goal_id,
                cycle=cycle,
                comparator=self.comparator,
                target=self.target,
                sustain_periods=self.sustain_periods,
                measure=self.measure,
                record=self.record,
                evaluate=self.evaluate,
                now=attempted_at,
            )
            self.receipts.append(receipt)
            if receipt.sustained:
                return self.receipts
        self._escalate(already_exhausted=False)
        return self.receipts


__all__ = [
    "CycleReceipt",
    "DryRunController",
    "EscalationReceipt",
    "EvaluateFn",
    "Measurement",
    "MeasureFn",
    "PaceFn",
    "PacingReceipt",
    "RecordFn",
    "RefusalReceipt",
    "meets_setpoint",
    "run_cycle",
]
