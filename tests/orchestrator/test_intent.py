"""Natural-language front door parsing and executor-intent application."""

from __future__ import annotations

from typing import Any

import pytest

from omniagentos.orchestrator import intent
from omniagentos.orchestrator.contracts import ExecutorTier, OverridePins
from omniagentos.orchestrator.intent import OrchestrationRequest, parse_intent
from omniagentos.orchestrator.tiers import resolve_execution


@pytest.mark.parametrize(
    ("goal", "fable_result", "expected"),
    [
        (
            "I need this done fast",
            {"task": "Do this", "priority": "fast"},
            {"priority": "fast", "executor": "fusion", "effort": None},
        ),
        (
            "Good quality is important",
            {"task": "Do this well", "priority": "quality"},
            {"priority": "quality", "executor": "fusion", "effort": None},
        ),
        (
            "Use GPT-5.6-Sol for this",
            {"task": "Do this", "executor": "gpt-5.6-sol"},
            {"priority": "balanced", "executor": "gpt-5.6-sol", "effort": None},
        ),
        (
            "Max reasoning effort needed",
            {"task": "Do this", "effort": "max"},
            {"priority": "balanced", "executor": "fusion", "effort": "max"},
        ),
        (
            "Balanced approach, use Opus",
            {"task": "Do this", "priority": "balanced", "executor": "opus"},
            {"priority": "balanced", "executor": "opus", "effort": None},
        ),
        (
            "Refactor the greeter",
            {"task": "Refactor the greeter"},
            {"priority": "balanced", "executor": "fusion", "effort": None},
        ),
    ],
)
def test_parse_intent_happy_paths(
    monkeypatch: pytest.MonkeyPatch,
    goal: str,
    fable_result: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_fable(prompt: str, schema: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append({"prompt": prompt, "schema": schema, **kwargs})
        return fable_result

    monkeypatch.setattr(intent, "run_fable_json", fake_fable)

    request = parse_intent(goal)

    assert request.priority == expected["priority"]
    assert request.executor == expected["executor"]
    assert request.effort == expected["effort"]
    assert calls[0]["effort"] == "medium"
    assert calls[0]["max_turns"] == 2
    assert calls[0]["wall_ms"] == 120_000
    assert goal in calls[0]["prompt"]
    assert calls[0]["schema"]["required"] == ["task"]


def test_parse_intent_degrades_when_fable_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(intent, "run_fable_json", lambda *args, **kwargs: None)

    request = parse_intent("Use Opus quickly")

    assert request == OrchestrationRequest(
        task="Use Opus quickly",
        priority="balanced",
        executor="fusion",
        effort=None,
        pins=OverridePins(),
    )
    assert "Fable unavailable; parsing goal with heuristic" in caplog.text


def test_parse_intent_coerces_incomplete_or_invalid_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        intent,
        "run_fable_json",
        lambda *args, **kwargs: {
            "priority": "unexpected",
            "executor": "gpt-9",
            "effort": "extreme",
            "pins": "not-an-object",
        },
    )

    request = parse_intent("Raw task")

    assert request.task == "Raw task"
    assert request.priority == "balanced"
    assert request.executor == "fusion"
    assert request.effort is None
    assert request.pins == OverridePins()


def test_executor_model_pins_do_not_replace_tier_selection() -> None:
    simple_fusion = resolve_execution(
        "balanced", "simple", OverridePins(executor_model_or_lineage="fusion")
    )
    complex_fusion = resolve_execution(
        "balanced", "complex", OverridePins(executor_model_or_lineage="fusion")
    )
    assert simple_fusion.executor_tier is ExecutorTier.CHEAP
    assert complex_fusion.executor_tier is ExecutorTier.FUSION

    sol = resolve_execution(
        "balanced", "simple", OverridePins(executor_model_or_lineage="gpt-5.6-sol")
    )
    assert sol.executor_tier is ExecutorTier.CHEAP
    assert sol.executor_model == "gpt-5.6-sol"

    opus = resolve_execution("quality", "simple", OverridePins(executor_model_or_lineage="opus"))
    assert opus.executor_tier is ExecutorTier.FUSION_ULTRA
    assert opus.executor_model == "opus"

    terra = resolve_execution(
        "balanced",
        "simple",
        OverridePins(executor_model_or_lineage="gpt-5.6-terra", executor_effort="ultrabuild"),
    )
    assert terra.executor_tier is ExecutorTier.FUSION_ULTRA
    assert terra.executor_model == "gpt-5.6-terra"
