"""Natural-language intake parsing for the Orchestrator front door.

This module only extracts operator intent.  Tier, lane, and final model resolution
remain owned by :mod:`omniagentos.orchestrator.tiers` and the orchestrator core.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, cast

from omniagentos.orchestrator.contracts import OverridePins, Priority

LOG = logging.getLogger(__name__)

ExecutorIntent = Literal[
    "fusion",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "opus",
]
Effort = Literal["medium", "high", "xhigh", "max"]

_VALID_PRIORITIES = frozenset({"fast", "balanced", "quality"})
_VALID_EXECUTORS = frozenset({"fusion", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "opus"})
_VALID_EFFORTS = frozenset({"medium", "high", "xhigh", "max"})

_INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["task"],
    "properties": {
        "task": {
            "type": "string",
            "description": "The core goal/task text, cleaned up if the user's phrasing was vague",
        },
        "priority": {
            "type": "string",
            "enum": ["fast", "balanced", "quality"],
            "description": (
                "User's implied priority: fast=cheap/quick, balanced=good-enough, "
                "quality=best result"
            ),
        },
        "executor": {
            "type": "string",
            "enum": ["fusion", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "opus"],
            "description": "Desired executor model/lineage, or fusion if user didn't specify",
        },
        "effort": {
            "type": ["string", "null"],
            "enum": ["medium", "high", "xhigh", "max", None],
            "description": (
                "Explicit effort override if parsing a request like 'I want max reasoning' "
                "or null if not specified"
            ),
        },
        "pins": {
            "type": "object",
            "properties": {
                "planner_model": {"type": ["string", "null"]},
                "planner_effort": {"type": ["string", "null"]},
                "executor_model_or_lineage": {"type": ["string", "null"]},
                "executor_effort": {"type": ["string", "null"]},
            },
        },
    },
}


def run_fable_json(
    prompt: str,
    schema: dict[str, Any],
    *,
    effort: str,
    max_turns: int,
    wall_ms: int,
) -> dict[str, Any] | None:
    """Lazily call the shared Fable seam without reviving the intake/API import cycle."""
    from omniagentos.intake.fable import run_fable_json as _run_fable_json

    return _run_fable_json(
        prompt,
        schema,
        effort=effort,
        max_turns=max_turns,
        wall_ms=wall_ms,
    )


@dataclass(slots=True)
class OrchestrationRequest:
    """Parsed from goal text via Fable; the natural-language front-door output."""

    task: str
    priority: Priority
    executor: ExecutorIntent
    effort: Effort | None
    pins: OverridePins


def _prompt(text: str) -> str:
    return "\n".join(
        [
            "You parse natural-language goals for the OmniAgentOS Orchestrator.",
            "Extract intent only; do not resolve an executor tier or perform the task.",
            "Return JSON matching the supplied schema.",
            "",
            "Detect priority signals such as fast, urgent, quick, balanced, quality,",
            "best, and thorough. Detect executor names Sol, Terra, Luna, Opus, or",
            "Fusion, normalizing them to the schema enum. Detect explicit reasoning",
            "effort medium, high, xhigh, or max. Put direct planner/executor overrides",
            "in pins. For every missing signal use priority=balanced, executor=fusion,",
            "effort=null, and empty pins. Preserve the concrete core task in task while",
            "removing only routing language when that makes the goal clearer.",
            "",
            f"USER GOAL:\n{text}",
        ]
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_pins(value: Any) -> OverridePins:
    if not isinstance(value, dict):
        return OverridePins()
    return OverridePins(
        planner_model=_optional_text(value.get("planner_model")),
        planner_effort=cast(Any, _optional_text(value.get("planner_effort"))),
        executor_model_or_lineage=_optional_text(value.get("executor_model_or_lineage")),
        executor_effort=_optional_text(value.get("executor_effort")),
    )


def _fallback(text: str) -> OrchestrationRequest:
    return OrchestrationRequest(
        task=text,
        priority="balanced",
        executor="fusion",
        effort=None,
        pins=OverridePins(),
    )


def parse_intent(text: str) -> OrchestrationRequest:
    """Parse a goal into intent knobs, degrading safely when Fable is unavailable.

    This is deliberately a parser, not a resolver.  The returned executor and pins
    are merely signals for the caller; the orchestrator core still decides the
    concrete tier, lane, and model.
    """
    try:
        raw = run_fable_json(
            _prompt(text),
            _INTENT_SCHEMA,
            effort="medium",
            max_turns=2,
            wall_ms=120_000,
        )
    except Exception:  # noqa: BLE001 -- the natural-language front door never hard-fails.
        LOG.warning("Fable unavailable; parsing goal with heuristic", exc_info=True)
        return _fallback(text)

    if not isinstance(raw, dict):
        LOG.warning("Fable unavailable; parsing goal with heuristic")
        return _fallback(text)

    task = _optional_text(raw.get("task")) or text

    priority_value = str(raw.get("priority", "balanced")).strip().lower()
    priority: Priority = (
        cast(Priority, priority_value) if priority_value in _VALID_PRIORITIES else "balanced"
    )

    executor_value = str(raw.get("executor", "fusion")).strip().lower()
    executor: ExecutorIntent = (
        cast(ExecutorIntent, executor_value) if executor_value in _VALID_EXECUTORS else "fusion"
    )

    effort_value = _optional_text(raw.get("effort"))
    normalized_effort = effort_value.lower() if effort_value is not None else None
    effort = cast(Effort, normalized_effort) if normalized_effort in _VALID_EFFORTS else None

    return OrchestrationRequest(
        task=task,
        priority=priority,
        executor=executor,
        effort=effort,
        pins=_coerce_pins(raw.get("pins")),
    )


__all__ = ["Effort", "ExecutorIntent", "OrchestrationRequest", "parse_intent"]
