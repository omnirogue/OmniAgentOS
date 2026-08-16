"""Mini-SWE must not record unknown cost as 0.0.

Unknown spend written as 0.0 is the same defect class that let a $6.33 run
pass a $4.00 cap: a non-measurement that looks like free. contracts.AgentUsage
uses ``cost_usd: float | None = None``; fusion and openhands already leave
unknown cost as None. Mini-SWE must match.

Counterfeits these tests reject:
- Always set cost_usd=None even when the CLI reported a real total_cost_usd.
- Set cost_usd=0.0 with estimated=False (claims a measured free run).
- On the error path, return cost_usd=0.0 (unknown still presented as free).
"""

from __future__ import annotations

from typing import Any

import pytest

from omniagentos.contracts import AgentInput, AgentUsage, ResultStatus
from omniagentos.harnesses.miniswe.adapter import MiniSweAdapter
from omniagentos.harnesses.miniswe.cli_model import _usage_from_payload


def test_usage_from_payload_unknown_cost_is_none_not_zero() -> None:
    """Estimator path: no payload → cost is unknown, not $0.00."""
    cost, _in_t, _out_t, estimated, source = _usage_from_payload(None, "prompt", "out")
    assert cost is None, f"unknown cost recorded as {cost!r}; None is the honest value"
    assert estimated is True
    assert source == "estimator"


def test_usage_from_payload_tokens_without_cost_leaves_cost_unknown() -> None:
    """Tokens without total_cost_usd: cost stays None (mixed), not 0.0."""
    payload = {
        "usage": {"input_tokens": 10, "output_tokens": 4},
        # deliberately no total_cost_usd
    }
    cost, in_t, out_t, estimated, source = _usage_from_payload(payload, "p", "o")
    assert in_t == 10 and out_t == 4
    assert cost is None, f"tokens-only report forged cost={cost!r}"
    assert estimated is True
    assert source == "mixed"


def test_usage_from_payload_reported_zero_cost_is_preserved() -> None:
    """A real CLI report of $0.00 must remain 0.0 (genuine free run).

    Counterfeit: blanket ``cost is None if estimated else cost`` that also
    erases an explicit free report. Only *missing* cost becomes None.
    """
    payload = {
        "usage": {"input_tokens": 3, "output_tokens": 1},
        "total_cost_usd": 0.0,
    }
    cost, _i, _o, estimated, source = _usage_from_payload(payload, "p", "o")
    assert cost == 0.0
    assert estimated is False
    assert source == "cli-report"


def test_usage_from_payload_reported_nonzero_cost() -> None:
    payload = {
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "total_cost_usd": 0.42,
    }
    cost, _i, _o, estimated, source = _usage_from_payload(payload, "p", "o")
    assert cost == pytest.approx(0.42)
    assert estimated is False
    assert source == "cli-report"


def test_adapter_error_path_unknown_cost_is_none() -> None:
    """Exception during run: we spent an unknown amount, not proven $0.00."""
    adapter = MiniSweAdapter()

    def boom(_input: AgentInput) -> tuple[dict[str, Any], Any]:
        raise RuntimeError("minisweagent not installed")

    adapter._run_agent = boom  # type: ignore[method-assign]
    result = adapter.run(AgentInput(run_id="r1", task_id="t1", prompt="hello"))
    assert result.status is ResultStatus.ERROR
    assert result.usage.cost_usd is None, (
        f"error path recorded cost_usd={result.usage.cost_usd!r}; unknown must be None"
    )
    assert result.usage.estimated is True


def test_adapter_usage_from_model_estimated_cost_is_none() -> None:
    """When the model only has estimator totals, cost_usd must be None."""
    adapter = MiniSweAdapter()

    class _EstModel:
        n_calls = 2
        input_tokens = 40
        output_tokens = 12
        cost_usd = None  # honest unknown
        estimated = True
        source = "estimator"

    usage = adapter._usage_from_model(_EstModel(), "prompt text", "submission", 100)
    assert isinstance(usage, AgentUsage)
    assert usage.cost_usd is None
    assert usage.estimated is True
    assert usage.source == "estimator"


def test_adapter_usage_from_model_zero_cost_with_estimator_flag_is_none() -> None:
    """ClaudeCliModel currently seeds cost_usd=0.0 while estimated=True.

    That seed is not a provider report. Mapping it through as 0.0 is exactly
    the flattering non-measurement. When estimated (or source is estimator),
    a 0.0 seed must become None unless source is a real cli-report with
    measured free cost — which is tested separately via _usage_from_payload.
    """
    adapter = MiniSweAdapter()

    class _SeededModel:
        n_calls = 1
        input_tokens = 10
        output_tokens = 5
        cost_usd = 0.0  # seed, not a measurement
        estimated = True
        source = "estimator"

    usage = adapter._usage_from_model(_SeededModel(), "p", "s", 50)
    assert usage.cost_usd is None, (
        f"estimator seed 0.0 leaked as measured free: cost_usd={usage.cost_usd!r}"
    )


def test_adapter_usage_from_model_cli_report_cost_preserved() -> None:
    """Real cli-report cost must not be erased by the unknown→None fix."""
    adapter = MiniSweAdapter()

    class _Reported:
        n_calls = 3
        input_tokens = 80
        output_tokens = 20
        cost_usd = 1.25
        estimated = False
        source = "cli-report"

    usage = adapter._usage_from_model(_Reported(), "p", "s", 90)
    assert usage.cost_usd == pytest.approx(1.25)
    assert usage.estimated is False
    assert usage.source == "cli-report"
