"""D10 Mode dial: the gemini planner rung + speed→chain mapping (fallback.py).

Fast Mode plans on gemini-3.6-flash through the CLI_GEMINI adapter with Fable
as the fallback; the gemini rung falls through the chain on ANY failure —
limit/unavailable AND format breaks — because planning must never silently
degrade to heuristics while a Fable rung is still available. The claude/codex
rungs keep the existing don't-waste-fallback rule.
"""

from __future__ import annotations

from typing import Any

import pytest

from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    AgentUsage,
    HarnessType,
    ResultStatus,
)
from omniagentos.intake.fallback import (
    FAST_PLANNER_MODEL,
    run_with_fallback,
    speed_planner_chain,
    speed_planner_effort,
    speed_planner_llm,
)


def _result(
    status: ResultStatus,
    output_json: dict | None = None,
    error: str | None = None,
) -> AgentResult:
    return AgentResult(
        status=status,
        output_json=output_json,
        error=error,
        usage=AgentUsage(wall_ms=100, turns=1, estimated=True),
    )


class _FakeAdapter:
    def __init__(self, results: list[AgentResult | Exception]) -> None:
        self.results = list(results)
        self.inputs: list[AgentInput] = []

    def run(self, input_obj: AgentInput) -> AgentResult:
        self.inputs.append(input_obj)
        outcome = self.results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Registry:
    """resolve_adapter stand-in recording which harness each rung resolved."""

    def __init__(self, adapters: dict[HarnessType, _FakeAdapter]) -> None:
        self.adapters = adapters
        self.resolved: list[HarnessType] = []

    def __call__(self, harness: HarnessType) -> _FakeAdapter:
        self.resolved.append(harness)
        return self.adapters[harness]


@pytest.fixture
def patch_registry(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _install(adapters: dict[HarnessType, _FakeAdapter]) -> _Registry:
        registry = _Registry(adapters)
        monkeypatch.setattr("omniagentos.adapters.registry.resolve_adapter", registry)
        return registry

    return _install


class TestSpeedChains:
    def test_mapping(self) -> None:
        assert speed_planner_chain("fast") == "gemini:fable"
        assert speed_planner_chain("auto") == "fable:opus:sol"
        assert speed_planner_chain("ultra") == "fable"

    def test_unknown_and_none_default_to_auto(self) -> None:
        assert speed_planner_chain(None) == "fable:opus:sol"
        assert speed_planner_chain("warp") == "fable:opus:sol"

    def test_effort_overrides(self) -> None:
        # D10: Auto = Fable X High; ultra = max; fast keeps the caller's
        # effort (the gemini CLI has no effort flag — a documented no-op).
        assert speed_planner_effort("auto") == "xhigh"
        assert speed_planner_effort("ultra") == "max"
        assert speed_planner_effort("fast") is None


class TestGeminiRung:
    def test_gemini_success_serves_without_fable(self, patch_registry: Any) -> None:
        gemini = _FakeAdapter([_result(ResultStatus.OK, output_json={"from": "gemini"})])
        fable = _FakeAdapter([])
        registry = patch_registry({HarnessType.CLI_GEMINI: gemini, HarnessType.CLI_CLAUDE: fable})

        out = run_with_fallback("plan it", {}, chain="gemini:fable")

        assert out == {"from": "gemini"}
        assert registry.resolved == [HarnessType.CLI_GEMINI]
        # The explicit model id goes straight to the adapter — no registry
        # lookup, so a stale var/modelintel/registry.json cannot break it.
        assert gemini.inputs[0].model == FAST_PLANNER_MODEL

    def test_gemini_limit_error_falls_back_to_fable(self, patch_registry: Any) -> None:
        gemini = _FakeAdapter([_result(ResultStatus.ERROR, error="429 Too Many Requests")])
        fable = _FakeAdapter([_result(ResultStatus.OK, output_json={"from": "fable"})])
        registry = patch_registry({HarnessType.CLI_GEMINI: gemini, HarnessType.CLI_CLAUDE: fable})

        out = run_with_fallback("plan it", {}, chain="gemini:fable")

        assert out == {"from": "fable"}
        assert registry.resolved == [HarnessType.CLI_GEMINI, HarnessType.CLI_CLAUDE]

    def test_gemini_format_break_falls_through_never_heuristics(self, patch_registry: Any) -> None:
        """A gemini format break (non-retryable error class) still falls through
        the chain — Fast Mode planning must not silently degrade to heuristics
        while Fable is available."""
        gemini = _FakeAdapter(
            [_result(ResultStatus.ERROR, error="Gemini output contained no JSON envelope")]
        )
        fable = _FakeAdapter([_result(ResultStatus.OK, output_json={"from": "fable"})])
        patch_registry({HarnessType.CLI_GEMINI: gemini, HarnessType.CLI_CLAUDE: fable})

        out = run_with_fallback("plan it", {}, chain="gemini:fable")

        assert out == {"from": "fable"}

    def test_gemini_ok_without_structured_output_falls_through(self, patch_registry: Any) -> None:
        gemini = _FakeAdapter([_result(ResultStatus.OK, output_json=None)])
        fable = _FakeAdapter([_result(ResultStatus.OK, output_json={"from": "fable"})])
        patch_registry({HarnessType.CLI_GEMINI: gemini, HarnessType.CLI_CLAUDE: fable})

        out = run_with_fallback("plan it", {}, chain="gemini:fable")

        assert out == {"from": "fable"}

    def test_gemini_gets_no_effort_metadata(self, patch_registry: Any) -> None:
        """The gemini CLI has no effort flag: effort is a documented no-op —
        never faked into metadata. The fable fallback rung still gets it."""
        gemini = _FakeAdapter([_result(ResultStatus.ERROR, error="rate limit")])
        fable = _FakeAdapter([_result(ResultStatus.OK, output_json={"ok": 1})])
        patch_registry({HarnessType.CLI_GEMINI: gemini, HarnessType.CLI_CLAUDE: fable})

        run_with_fallback("plan it", {}, effort="high", chain="gemini:fable")

        assert "effort" not in gemini.inputs[0].metadata
        assert fable.inputs[0].metadata["effort"] == "high"

    def test_fable_bad_answer_still_does_not_waste_fallback(self, patch_registry: Any) -> None:
        """The don't-waste-fallback rule is untouched for the claude rungs."""
        fable = _FakeAdapter(
            [_result(ResultStatus.ERROR, error="response JSON is missing required keys")]
        )
        registry = patch_registry({HarnessType.CLI_CLAUDE: fable})

        out = run_with_fallback("plan it", {}, chain="fable:opus")

        assert out is None
        assert registry.resolved == [HarnessType.CLI_CLAUDE]

    def test_unknown_chain_entries_are_skipped(self, patch_registry: Any) -> None:
        fable = _FakeAdapter([_result(ResultStatus.OK, output_json={"ok": 1})])
        registry = patch_registry({HarnessType.CLI_CLAUDE: fable})

        out = run_with_fallback("plan it", {}, chain="warp:fable")

        assert out == {"ok": 1}
        assert registry.resolved == [HarnessType.CLI_CLAUDE]


class TestSpeedPlannerLLM:
    def test_auto_plans_on_fable_at_xhigh(self, patch_registry: Any) -> None:
        fable = _FakeAdapter([_result(ResultStatus.OK, output_json={"ok": 1})])
        registry = patch_registry({HarnessType.CLI_CLAUDE: fable})

        llm = speed_planner_llm("auto")
        out = llm("plan it", {}, "high")

        assert out == {"ok": 1}
        assert registry.resolved == [HarnessType.CLI_CLAUDE]
        assert fable.inputs[0].metadata["effort"] == "xhigh"

    def test_fast_plans_on_gemini_first(self, patch_registry: Any) -> None:
        gemini = _FakeAdapter([_result(ResultStatus.OK, output_json={"ok": 1})])
        registry = patch_registry({HarnessType.CLI_GEMINI: gemini})

        llm = speed_planner_llm("fast")
        out = llm("plan it", {}, "high")

        assert out == {"ok": 1}
        assert registry.resolved == [HarnessType.CLI_GEMINI]
        assert gemini.inputs[0].model == FAST_PLANNER_MODEL

    def test_ultra_plans_on_fable_alone_at_max(self, patch_registry: Any) -> None:
        limit = _result(ResultStatus.ERROR, error="rate limit")
        fable = _FakeAdapter([limit])
        registry = patch_registry({HarnessType.CLI_CLAUDE: fable})

        llm = speed_planner_llm("ultra")
        out = llm("plan it", {}, "high")

        # Chain is fable ONLY: a limit error exhausts it (heuristics upstream).
        assert out is None
        assert registry.resolved == [HarnessType.CLI_CLAUDE]
        assert fable.inputs[0].metadata["effort"] == "max"


class TestOrchestratorPlannerLineage:
    """orchestrator.core._model_planner_llm resolves the adapter by lineage."""

    def test_gemini_pin_plans_on_gemini_with_fable_fallback(self, patch_registry: Any) -> None:
        from omniagentos.orchestrator.core import _model_planner_llm

        gemini = _FakeAdapter([_result(ResultStatus.ERROR, error="no JSON envelope")])
        fable = _FakeAdapter([_result(ResultStatus.OK, output_json={"from": "fable"})])
        registry = patch_registry({HarnessType.CLI_GEMINI: gemini, HarnessType.CLI_CLAUDE: fable})

        llm = _model_planner_llm("gemini-3.6-flash")
        out = llm("plan it", {}, "high")

        assert out == {"from": "fable"}
        assert registry.resolved == [HarnessType.CLI_GEMINI, HarnessType.CLI_CLAUDE]
        # The pinned model id rode into the gemini adapter explicitly.
        assert gemini.inputs[0].model == "gemini-3.6-flash"

    def test_claude_pin_keeps_the_fable_path(
        self, patch_registry: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omniagentos.orchestrator.core import _model_planner_llm

        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            "omniagentos.intake.fable.run_fable_json",
            lambda prompt, schema, **kw: calls.append(kw) or {"ok": 1},
        )

        llm = _model_planner_llm("opus")
        out = llm("plan it", {}, "max")

        assert out == {"ok": 1}
        assert calls[0]["model"] == "opus"
        assert calls[0]["effort"] == "max"
