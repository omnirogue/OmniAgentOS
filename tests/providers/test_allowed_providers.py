"""D5: per-run allowed_providers cannot leak to a banned Claude CLI."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from omniagentos.contracts import AgentResult, AgentUsage, HarnessType, ResultStatus
from omniagentos.dispatch.providers import (
    allowed_providers_from_params,
    assert_dispatch_provider,
    filter_dispatch_candidates,
)
from omniagentos.intake.fallback import Rung, run_with_fallback
from omniagentos.providers.constraints import (
    ALLOWED_PROVIDERS_MODE_ENV,
    ProviderNotAllowed,
    filter_allowed,
    normalize_provider,
)
from omniagentos.routing.workers import WorkerEndpoint, select_worker

PIN = ["codex", "grok", "gemini"]


@pytest.fixture(autouse=True)
def _mode_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ALLOWED_PROVIDERS_MODE_ENV, raising=False)


def test_mode_defaults_off() -> None:
    from omniagentos.providers.constraints import allowed_providers_mode

    assert allowed_providers_mode() == "off"


def test_off_mode_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWED_PROVIDERS_MODE_ENV, "off")
    cands = ["claude", "codex", "grok"]
    assert filter_allowed(cands, PIN, stage="t") == cands


def test_shadow_logs_but_keeps(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv(ALLOWED_PROVIDERS_MODE_ENV, "shadow")
    import logging

    caplog.set_level(logging.INFO, logger="omniagentos.providers.constraints")
    cands = ["claude", "codex", "grok"]
    out = filter_allowed(cands, PIN, stage="shadow_test")
    assert out == cands
    assert any("allowed_providers violation" in r.message for r in caplog.records)


def test_enforce_drops_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWED_PROVIDERS_MODE_ENV, "enforce")
    out = filter_allowed(["claude", "codex", "grok"], PIN, stage="t")
    assert out == ["codex", "grok"]


def test_enforce_exhaustion_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWED_PROVIDERS_MODE_ENV, "enforce")
    with pytest.raises(ProviderNotAllowed) as ei:
        filter_allowed(["claude", "fable", "opus"], PIN, stage="planner_fallback")
    assert ei.value.stage == "planner_fallback"
    assert "claude" in ei.value.rejected or any("claude" in r for r in ei.value.rejected)


def test_none_allowlist_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWED_PROVIDERS_MODE_ENV, "enforce")
    assert filter_allowed(["claude"], None, stage="t") == ["claude"]


def test_normalize_aliases() -> None:
    assert normalize_provider("fable") == "claude"
    assert normalize_provider("opus") == "claude"
    assert normalize_provider("sol") == "codex"
    assert normalize_provider("cli-claude") == "claude"
    assert normalize_provider("gemini-3.6-flash") == "gemini"


def test_worker_rotation_cannot_pick_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWED_PROVIDERS_MODE_ENV, "enforce")
    endpoints = [
        WorkerEndpoint("claude:a", "claude", "a", "terminal_cli"),
        WorkerEndpoint("codex:b", "codex", "b", "terminal_cli"),
        WorkerEndpoint("grok:c", "grok", "c", "terminal_cli"),
    ]
    sel = select_worker(
        tier="fast",
        effort="low",
        preferred_providers=["claude", "codex", "grok"],
        endpoints=endpoints,
        allowed_providers=PIN,
    )
    assert sel.endpoint is not None
    assert sel.endpoint.provider != "claude"
    assert sel.endpoint.provider in PIN


def test_worker_rotation_all_banned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWED_PROVIDERS_MODE_ENV, "enforce")
    endpoints = [
        WorkerEndpoint("claude:a", "claude", "a", "terminal_cli"),
        WorkerEndpoint("claude:b", "claude", "b", "terminal_cli"),
    ]
    sel = select_worker(
        tier="fast",
        effort="low",
        preferred_providers=["claude"],
        endpoints=endpoints,
        allowed_providers=PIN,
    )
    assert sel.endpoint is None
    assert sel.reason == "provider_not_allowed"


def test_planner_fallback_never_calls_claude_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin codex/grok/gemini; patch Claude CLI entry to fail if called."""
    monkeypatch.setenv(ALLOWED_PROVIDERS_MODE_ENV, "enforce")

    claude_calls: list[str] = []

    class _ClaudeBoom:
        def run(self, *_a: Any, **_k: Any) -> AgentResult:
            claude_calls.append("called")
            raise AssertionError("Claude CLI must not be invoked under the pin")

    class _OkAdapter:
        def run(self, input_obj: Any) -> AgentResult:
            return AgentResult(
                status=ResultStatus.OK,
                output_json={"ok": True},
                usage=AgentUsage(wall_ms=10, turns=1, output_tokens=5, estimated=True),
            )

    def _resolve(harness: Any) -> Any:
        key = str(harness)
        if "claude" in key or harness == HarnessType.CLI_CLAUDE:
            return _ClaudeBoom()
        return _OkAdapter()

    monkeypatch.setattr(
        "omniagentos.adapters.registry.resolve_adapter",
        _resolve,
    )
    # Force a chain that would start with claude rungs if unfiltered.
    chain = "fable:opus:sol:grok:gemini"
    # Filter should drop fable/opus (claude); sol/grok/gemini remain.
    # Make _resolve_chain return known rungs by using the string form.
    result = run_with_fallback(
        "return json",
        {"type": "object"},
        chain=chain,
        allowed_providers=PIN,
        max_turns=1,
        wall_ms=5_000,
    )
    assert claude_calls == []
    assert result == {"ok": True}


def test_planner_fallback_raises_when_only_claude_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ALLOWED_PROVIDERS_MODE_ENV, "enforce")

    def _resolve(_harness: Any) -> Any:
        raise AssertionError("no adapter should run")

    monkeypatch.setattr("omniagentos.adapters.registry.resolve_adapter", _resolve)
    with pytest.raises(ProviderNotAllowed):
        run_with_fallback(
            "x",
            {"type": "object"},
            chain="fable:opus",
            allowed_providers=PIN,
        )


def test_dispatch_params_parse_and_spawn_assert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWED_PROVIDERS_MODE_ENV, "enforce")
    params = {"allowed_providers": ["codex", "grok", "gemini"]}
    assert allowed_providers_from_params(params) == ["codex", "grok", "gemini"]
    kept = filter_dispatch_candidates(
        [MagicMock(provider="claude"), MagicMock(provider="codex")],
        params,
    )
    assert len(kept) == 1
    assert kept[0].provider == "codex"
    with pytest.raises(ProviderNotAllowed):
        assert_dispatch_provider("claude", params, stage="swarm_worker_spawn")
    assert_dispatch_provider("codex", params, stage="swarm_worker_spawn")


def test_rung_provider_of() -> None:
    from omniagentos.providers.constraints import provider_of

    r = Rung("fable", HarnessType.CLI_CLAUDE, "fable", provider="claude", cli="claude")
    assert provider_of(r) == "claude"
    r2 = Rung("sol", HarnessType.CLI_CODEX, "gpt-5.6-sol", provider="codex", cli="codex")
    assert provider_of(r2) == "codex"
