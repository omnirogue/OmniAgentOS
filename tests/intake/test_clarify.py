"""Unit tests for the clarifying agent's turn logic (LLM seam injected)."""

from __future__ import annotations

from typing import Any

from omniagentos.intake.service import clarify_intake


def _fake_llm(reply: dict[str, Any] | None):
    def _llm(_prompt: str, _schema: dict[str, Any]) -> dict[str, Any] | None:
        return reply

    return _llm


def test_questions_first_round() -> None:
    result = clarify_intake(
        "build a thing",
        [],
        llm=_fake_llm({"mode": "questions", "questions": ["What outcome?", "Which project?"]}),
    )
    assert result.mode == "questions"
    assert 1 <= len(result.questions) <= 3
    assert result.round == 0
    assert result.forced is False


def test_spec_when_agent_has_enough() -> None:
    reply = {
        "mode": "spec",
        "spec": {
            "title": "Build X",
            "description": "Build the X widget",
            "acceptance_criteria": ["renders", "tested"],
            "suggested_priority": "HIGH",
            "suggested_discipline": "frontend",
        },
    }
    result = clarify_intake("build x", [{"q": "which?", "a": "the widget"}], llm=_fake_llm(reply))
    assert result.mode == "spec"
    assert result.spec is not None
    assert result.spec.title == "Build X"
    # priority normalized to lowercase within the allowed set.
    assert result.spec.suggested_priority == "high"
    assert result.spec.acceptance_criteria == ["renders", "tested"]


def test_round_budget_forces_a_spec() -> None:
    # The model keeps asking, but two answered rounds exhaust the budget: the
    # agent must stop looping and synthesize a spec.
    history = [{"q": "a", "a": "1"}, {"q": "b", "a": "2"}]
    result = clarify_intake(
        "draft request",
        history,
        llm=_fake_llm({"mode": "questions", "questions": ["one more?"]}),
    )
    assert result.mode == "spec"
    assert result.forced is True
    assert result.spec is not None
    assert result.spec.title


def test_heuristic_fallback_when_llm_unavailable() -> None:
    # round 0: no answers yet -> ask questions.
    r0 = clarify_intake("do the thing", [], llm=_fake_llm(None))
    assert r0.mode == "questions"
    assert r0.questions
    # round 1: an answer exists -> synthesize a spec deterministically.
    r1 = clarify_intake(
        "do the thing",
        [{"q": "outcome?", "a": "ship the report"}],
        llm=_fake_llm(None),
    )
    assert r1.mode == "spec"
    assert r1.spec is not None
    assert r1.spec.title
    assert r1.spec.acceptance_criteria


def test_malformed_spec_falls_back_to_heuristic() -> None:
    # mode=spec but no usable title -> heuristic spec, not a crash.
    result = clarify_intake(
        "make a dashboard",
        [{"q": "?", "a": "yes"}],
        llm=_fake_llm({"mode": "spec", "spec": {"title": ""}}),
    )
    assert result.mode == "spec"
    assert result.spec is not None
    assert result.spec.title
