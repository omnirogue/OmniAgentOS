"""The api-tier adapter's wall-clock budget and candidate cap.

REGRESSION (critic finding 5). ``OpenAiCompatibleAdapter.run()`` computed ONE
timeout from ``budget.wall_ms_max`` and handed that FULL value to every
candidate, so a rung the planner budgeted 30s for could spend 30s per model —
N candidates, N x the budget, while the caller believed it had a bound. There is
now a single monotonic deadline: each attempt gets only the time still left on
it, attempts stop when nothing useful remains, and the candidate list itself is
capped (:data:`MAX_API_CANDIDATES`) so a long ``openrouter_models`` list cannot
turn one rung into a dozen sequential HTTP calls.

Entirely offline: ``requests.post`` is replaced by a fake that advances a fake
clock, so the "slow provider" is simulated, never waited on.
"""

from __future__ import annotations

from typing import Any

import pytest

from omniagentos.adapters import api_base as api_base_module
from omniagentos.adapters.api_base import (
    MAX_API_CANDIDATES,
    MIN_ATTEMPT_SECONDS,
    OpenAiCompatibleAdapter,
)
from omniagentos.contracts import AgentInput, BudgetSpec, ResultStatus, new_id
from omniagentos.routing.api_policy import API_PATH_OPENROUTER, ApiRoutePolicyError

#: Ten ids that all CLEAR the deny-list, so this file tests budget/cap only.
ALLOWED_MODELS = (
    "grok-4.5",
    "grok-4.3",
    "grok-build-0.1",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-pro",
    "x-ai/grok-4.5",
    "x-ai/grok-4.3",
    "google/gemini-3.6-flash",
    "google/gemini-3.1-pro",
)


class FakeClock:
    """A ``time`` stand-in: monotonic() only moves when a request "takes" time."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Adapter(OpenAiCompatibleAdapter):
    name = "fake-api"
    api_path = API_PATH_OPENROUTER
    requires_key = False

    def __init__(self, models: tuple[str, ...]) -> None:
        self._models = models

    def api_base(self) -> str:
        return "https://example.invalid/v1"

    def api_key(self) -> str | None:
        return "sk-test"

    def default_models(self) -> tuple[str, ...]:
        return self._models


def _input(model: str = "", wall_ms: int | None = 30_000) -> AgentInput:
    return AgentInput(
        run_id=new_id("run"),
        task_id=new_id("tsk"),
        prompt="plan it",
        model=model,
        budget=BudgetSpec(wall_ms_max=wall_ms),
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(api_base_module, "time", fake)
    return fake


@pytest.fixture
def burn_the_timeout(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> list[float]:
    """Every request consumes exactly the timeout it was given, then fails."""
    import requests

    timeouts: list[float] = []

    def _post(url: str, **kwargs: Any) -> Any:
        timeout = float(kwargs["timeout"])
        timeouts.append(timeout)
        clock.advance(timeout)
        raise RuntimeError("read timed out")

    monkeypatch.setattr(requests, "post", _post)
    return timeouts


class TestOneSharedDeadline:
    def test_candidates_share_the_budget_instead_of_each_getting_it(
        self, burn_the_timeout: list[float]
    ) -> None:
        result = _Adapter(ALLOWED_MODELS[:3]).run(_input(wall_ms=30_000))

        assert result.status == ResultStatus.ERROR
        # The bug: [30.0, 30.0, 30.0] — 90s spent against a 30s budget.
        assert sum(burn_the_timeout) <= 30.0
        assert burn_the_timeout[0] == pytest.approx(30.0)
        assert burn_the_timeout == sorted(burn_the_timeout, reverse=True)

    def test_the_first_candidate_still_gets_the_whole_budget(
        self, burn_the_timeout: list[float]
    ) -> None:
        """No fair-share division: one slow model may legitimately use it all."""
        _Adapter(ALLOWED_MODELS[:3]).run(_input(wall_ms=12_000))
        assert burn_the_timeout[0] == pytest.approx(12.0)

    def test_later_candidates_are_skipped_once_the_budget_is_gone(
        self, burn_the_timeout: list[float]
    ) -> None:
        result = _Adapter(ALLOWED_MODELS[:3]).run(_input(wall_ms=30_000))

        assert len(burn_the_timeout) == 1  # the first attempt consumed it all
        assert "budget exhausted" in (result.error or "")

    def test_a_fast_failure_leaves_room_for_the_next_candidate(
        self, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
    ) -> None:
        """Cheap failures must NOT be charged the whole budget."""
        import requests

        tried: list[tuple[str, float]] = []

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {"choices": [{"message": {"content": '{"ok": 1}'}}]}

        def _post(url: str, **kwargs: Any) -> Any:
            tried.append((kwargs["json"]["model"], float(kwargs["timeout"])))
            clock.advance(0.25)  # a fast connection refusal
            if len(tried) < 3:
                raise RuntimeError("connection refused")
            return _Response()

        monkeypatch.setattr(requests, "post", _post)

        result = _Adapter(ALLOWED_MODELS[:3]).run(_input(wall_ms=30_000))

        assert result.status == ResultStatus.OK
        assert [model for model, _ in tried] == list(ALLOWED_MODELS[:3])
        remaining = [timeout for _, timeout in tried]
        assert remaining[0] == pytest.approx(30.0)
        assert remaining[1] == pytest.approx(29.75)
        assert remaining[2] == pytest.approx(29.5)

    def test_a_tiny_budget_still_gets_one_attempt(self, burn_the_timeout: list[float]) -> None:
        _Adapter(ALLOWED_MODELS[:3]).run(_input(wall_ms=10))
        assert len(burn_the_timeout) == 1
        assert burn_the_timeout[0] == pytest.approx(MIN_ATTEMPT_SECONDS)

    def test_no_budget_falls_back_to_the_default_ceiling(
        self, burn_the_timeout: list[float]
    ) -> None:
        _Adapter(ALLOWED_MODELS[:3]).run(_input(wall_ms=None))
        assert burn_the_timeout[0] == pytest.approx(api_base_module.DEFAULT_TIMEOUT_SECONDS)
        assert sum(burn_the_timeout) <= api_base_module.DEFAULT_TIMEOUT_SECONDS


class TestCandidateCap:
    def test_a_long_configured_list_is_truncated(self) -> None:
        adapter = _Adapter(ALLOWED_MODELS)
        assert len(ALLOWED_MODELS) > MAX_API_CANDIDATES
        assert len(adapter.candidate_models(_input())) == MAX_API_CANDIDATES
        assert len(adapter.configured_models(_input())) == len(ALLOWED_MODELS)

    def test_at_most_the_cap_is_ever_attempted(
        self, monkeypatch: pytest.MonkeyPatch, clock: FakeClock
    ) -> None:
        import requests

        tried: list[str] = []

        def _post(url: str, **kwargs: Any) -> Any:
            tried.append(kwargs["json"]["model"])
            raise RuntimeError("boom")  # instant failure: budget is never the limit

        monkeypatch.setattr(requests, "post", _post)

        _Adapter(ALLOWED_MODELS).run(_input(wall_ms=600_000))

        assert len(tried) == MAX_API_CANDIDATES
        assert tried == list(ALLOWED_MODELS[:MAX_API_CANDIDATES])

    def test_a_requested_model_is_tried_first_and_counts_against_the_cap(self) -> None:
        adapter = _Adapter(ALLOWED_MODELS)
        candidates = adapter.candidate_models(_input(model="gemini-3.5-flash-lite"))
        assert candidates[0] == "gemini-3.5-flash-lite"
        assert len(candidates) == MAX_API_CANDIDATES

    def test_the_deny_list_still_covers_ids_the_cap_trimmed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A denied id hiding past the cap must still fail the call CLOSED."""
        import requests

        def _never(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
            raise AssertionError("a denied model must never reach the network")

        monkeypatch.setattr(requests, "post", _never)
        adapter = _Adapter((*ALLOWED_MODELS[:MAX_API_CANDIDATES], "google/claude-opus-5"))

        assert "google/claude-opus-5" not in adapter.candidate_models(_input())
        with pytest.raises(ApiRoutePolicyError):
            adapter.run(_input())
