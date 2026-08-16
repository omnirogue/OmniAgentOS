"""Budget enforcement for the direct OpenRouter HTTP fallback."""

from __future__ import annotations

from typing import Any

import pytest

from omniagentos.adapters import api_base as api_base_module
from omniagentos.adapters.openrouter import OpenRouterAdapter
from omniagentos.contracts import AgentInput, BudgetDecision, BudgetSpec, ResultStatus, new_id


def _input(cost_usd_max: float | None) -> AgentInput:
    return AgentInput(
        run_id=new_id("run"),
        task_id=new_id("tsk"),
        prompt="plan it",
        model="x-ai/grok-4.5",
        budget=BudgetSpec(cost_usd_max=cost_usd_max),
    )


@pytest.fixture
def openrouter(monkeypatch: pytest.MonkeyPatch) -> OpenRouterAdapter:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    return OpenRouterAdapter()


def _successful_response() -> Any:
    class _Response:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
            }

    return _Response()


def test_openrouter_blocked_when_cap_exceeded(
    monkeypatch: pytest.MonkeyPatch, openrouter: OpenRouterAdapter
) -> None:
    """A rejected budget decision stops the direct path before HTTP."""
    import requests

    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    monkeypatch.setattr(
        api_base_module,
        "budget_check",
        lambda *_args: BudgetDecision(allowed=False, reason="cost_usd 0.02 > cap 0.01"),
    )

    def _never(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("budget-rejected request reached HTTP")

    monkeypatch.setattr(requests, "post", _never)

    result = openrouter.run(_input(cost_usd_max=0.01))

    assert result.status == ResultStatus.ERROR
    assert "budget cap" in (result.error or "").lower()


def test_openrouter_allowed_when_under_cap(
    monkeypatch: pytest.MonkeyPatch, openrouter: OpenRouterAdapter
) -> None:
    import requests

    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    posted: list[dict[str, Any]] = []

    def _post(_url: str, **kwargs: Any) -> Any:
        posted.append(kwargs["json"])
        return _successful_response()

    monkeypatch.setattr(requests, "post", _post)

    result = openrouter.run(_input(cost_usd_max=1.0))

    assert posted
    assert result.status == ResultStatus.OK


def test_openrouter_advisory_mode_allows_any_cap(
    monkeypatch: pytest.MonkeyPatch, openrouter: OpenRouterAdapter
) -> None:
    import requests

    monkeypatch.delenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", raising=False)
    posted: list[dict[str, Any]] = []

    def _post(_url: str, **kwargs: Any) -> Any:
        posted.append(kwargs["json"])
        return _successful_response()

    monkeypatch.setattr(requests, "post", _post)
    monkeypatch.setattr(
        api_base_module,
        "budget_check",
        lambda *_args: (_ for _ in ()).throw(AssertionError("advisory mode must not check")),
    )

    result = openrouter.run(_input(cost_usd_max=0.001))

    assert posted
    assert result.status == ResultStatus.OK


def test_no_cap_specified_works_normally(
    monkeypatch: pytest.MonkeyPatch, openrouter: OpenRouterAdapter
) -> None:
    import requests

    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    posted: list[dict[str, Any]] = []

    def _post(_url: str, **kwargs: Any) -> Any:
        posted.append(kwargs["json"])
        return _successful_response()

    monkeypatch.setattr(requests, "post", _post)
    monkeypatch.setattr(
        api_base_module,
        "budget_check",
        lambda *_args: (_ for _ in ()).throw(AssertionError("uncapped request must not check")),
    )

    result = openrouter.run(_input(cost_usd_max=None))

    assert posted
    assert result.status == ResultStatus.OK
