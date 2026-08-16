from __future__ import annotations

import os

import pytest

from omniagentos.lab.curator.agent import live_agent_enabled, run_curation_agent


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIAGENTOS_CURATOR_LIVE_AGENT", raising=False)
    assert live_agent_enabled() is False
    assert run_curation_agent("prompt text", dry_run=False) is None


def test_dry_run_never_invokes_an_adapter_even_when_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CURATOR_LIVE_AGENT", "1")
    assert run_curation_agent("prompt text", dry_run=True) is None


def test_opt_in_env_var_must_be_exactly_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CURATOR_LIVE_AGENT", "true")
    assert live_agent_enabled() is False
    assert run_curation_agent("prompt text", dry_run=False) is None


def test_live_agent_runs_the_mock_adapter_with_a_scrubbed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CURATOR_LIVE_AGENT", "1")
    monkeypatch.setenv("OMNIAGENTOS_EVAL_PROTECTED", "/var/eval_protected.db")

    from omniagentos.contracts import AgentInput
    from omniagentos.mock_adapter import MockAdapter

    seen_env: dict[str, str] = {}
    original_run = MockAdapter.run

    def spy_run(self: MockAdapter, input: AgentInput) -> object:
        seen_env.update(os.environ)
        return original_run(self, input)

    monkeypatch.setattr(MockAdapter, "run", spy_run)

    result = run_curation_agent("prompt text", dry_run=False, harness="mock")

    assert result is not None
    assert result.status.value == "ok"
    assert "OMNIAGENTOS_EVAL_PROTECTED" not in seen_env
    # restored after the call
    assert os.environ["OMNIAGENTOS_EVAL_PROTECTED"] == "/var/eval_protected.db"


def test_live_agent_passes_the_prompt_through_to_the_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CURATOR_LIVE_AGENT", "1")
    result = run_curation_agent("curate this please", dry_run=False, harness="mock")
    assert result is not None
    assert result.output_text == "mock-ok"
