"""P1-COST-EDGE: exact adapter cost observations at every edge.

Required assertions (lane brief):
- decimal ``0.01144063`` maps exactly to ``11440630`` nanos and round-trips;
- billed failures retain served provider, served model, and exact cost;
- missing cost remains unknown, never numeric zero;
- strict model identity cannot be silently dropped;
- post-SIGKILL communicate/pipe reaping has a bounded terminal outcome.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

import pytest

from omniagentos.adapters.api_base import (
    OpenAiCompatibleAdapter,
    cost_from_usage_dict,
    parse_provider_cost,
)
from omniagentos.adapters.common import (
    POST_KILL_REAP_TIMEOUT_SECONDS,
    CliAdapter,
    CostQuality,
    build_cost_observation,
    normalize_provider_cost,
)
from omniagentos.adapters.openrouter import OpenRouterAdapter
from omniagentos.contracts import (
    AgentInput,
    BudgetSpec,
    CostObservation,
    ResultStatus,
    _parse_cost_usd_decimal,
    new_id,
)
from omniagentos.routing.api_policy import API_PATH_OPENROUTER

EXACT_COST = 0.01144063
EXACT_DECIMAL = "0.01144063"
EXACT_NANOS = 11_440_630
# Policy-allowed OpenRouter candidates (must pass assert_api_route_allowed).
ALLOWED_MODEL = "x-ai/grok-4.5"
ALLOWED_FALLBACK = "deepseek/deepseek-v4-pro"


class _ProbeAdapter(OpenAiCompatibleAdapter):
    name = "openrouter"
    api_path = API_PATH_OPENROUTER
    requires_key = False

    def __init__(self, base: str = "http://127.0.0.1:9/v1") -> None:
        self._base = base

    def api_base(self) -> str:
        return self._base

    def api_key(self) -> str | None:
        return "sk-test"

    def default_models(self) -> tuple[str, ...]:
        return (ALLOWED_MODEL, ALLOWED_FALLBACK)


def _agent_input(
    model: str = ALLOWED_MODEL,
    *,
    strict: bool = False,
    wall_ms: int = 30_000,
) -> AgentInput:
    meta: dict[str, Any] = {}
    if strict:
        meta["strict_model"] = True
    return AgentInput(
        run_id=new_id("run"),
        task_id=new_id("tsk"),
        prompt="probe cost",
        model=model,
        budget=BudgetSpec(wall_ms_max=wall_ms),
        metadata=meta,
    )


def _observation_from_result(result) -> dict[str, Any] | None:
    for receipt in result.receipts or []:
        if receipt.key == "cost_observation_json" and receipt.target:
            return json.loads(receipt.target)
    return None


# ---------------------------------------------------------------------------
# Decimal / nano round-trip
# ---------------------------------------------------------------------------


class TestDecimalRoundTrip:
    def test_exact_openrouter_cost_maps_to_nanos(self) -> None:
        cost, decimal_text, nanos = parse_provider_cost(EXACT_COST)
        assert cost == pytest.approx(EXACT_COST)
        assert decimal_text == EXACT_DECIMAL
        assert nanos == EXACT_NANOS

        preserved, nano2 = _parse_cost_usd_decimal(decimal_text)
        assert preserved == EXACT_DECIMAL
        assert nano2 == EXACT_NANOS

    def test_normalize_provider_cost_round_trips(self) -> None:
        norm = normalize_provider_cost(EXACT_COST)
        assert norm.quality == CostQuality.EXACT
        assert norm.cost_usd_decimal == EXACT_DECIMAL
        assert norm.cost_usd_nanos == EXACT_NANOS
        assert norm.cost_usd == pytest.approx(EXACT_COST)

        again = normalize_provider_cost(norm.cost_usd_decimal)
        assert again.cost_usd_nanos == EXACT_NANOS
        assert again.cost_usd_decimal == EXACT_DECIMAL

    def test_cost_from_usage_dict_reads_provider_cost_field(self) -> None:
        cost, decimal_text, nanos = cost_from_usage_dict(
            {"prompt_tokens": 3, "completion_tokens": 5, "cost": EXACT_COST}
        )
        assert cost == pytest.approx(EXACT_COST)
        assert decimal_text == EXACT_DECIMAL
        assert nanos == EXACT_NANOS

    def test_dropped_cost_field_is_unknown_not_zero(self) -> None:
        """When the provider cost key is absent, observation stays unknown.

        Negative mutation ``dropped-exact-cost``: stripping the cost field before
        normalization must not invent ``0.0``.
        """
        cost, decimal_text, nanos = cost_from_usage_dict(
            {"prompt_tokens": 3, "completion_tokens": 5}
        )
        assert cost is None
        assert decimal_text is None
        assert nanos is None


# ---------------------------------------------------------------------------
# Unknown vs exact zero
# ---------------------------------------------------------------------------


class TestUnknownVsZero:
    def test_missing_cost_is_none_not_zero(self) -> None:
        assert parse_provider_cost(None) == (None, None, None)
        assert normalize_provider_cost(None).quality == CostQuality.UNKNOWN
        assert normalize_provider_cost(None).cost_usd is None

    def test_exact_zero_is_preserved(self) -> None:
        cost, decimal_text, nanos = parse_provider_cost(0.0)
        assert cost == 0.0
        assert nanos == 0
        assert decimal_text in {"0", "0.0"}

        norm = normalize_provider_cost(0.0)
        assert norm.quality == CostQuality.EXACT
        assert norm.cost_usd == 0.0
        assert norm.cost_usd_nanos == 0

    def test_invalid_cost_is_unknown(self) -> None:
        assert parse_provider_cost("not-a-number") == (None, None, None)
        assert parse_provider_cost(float("nan")) == (None, None, None)
        assert parse_provider_cost(-0.01) == (None, None, None)
        assert normalize_provider_cost("not-a-number").quality == CostQuality.UNKNOWN

    def test_adapter_usage_preserves_unknown(self) -> None:
        adapter = OpenRouterAdapter()
        body = {
            "model": ALLOWED_MODEL,
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
        }
        usage = adapter._usage(_agent_input(), body, "ok", 7)
        assert usage.cost_usd is None, f"unknown collapsed to {usage.cost_usd!r}"
        assert usage.estimated is True

    def test_adapter_usage_preserves_exact_cost(self) -> None:
        adapter = OpenRouterAdapter()
        body = {
            "model": "served/model-id",
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 5,
                "cost": EXACT_COST,
            },
        }
        usage, receipts = adapter._usage_and_receipts(
            _agent_input(), body, "ok", 7, requested_model=ALLOWED_MODEL
        )
        assert usage.cost_usd == pytest.approx(EXACT_COST)
        assert usage.estimated is False
        assert usage.source == "cli-report"
        obs = None
        for r in receipts:
            if r.key == "cost_observation_json":
                obs = json.loads(r.target)
        assert obs is not None
        assert obs["cost_usd_nanos"] == EXACT_NANOS
        assert obs["cost_usd_decimal"] == EXACT_DECIMAL
        assert obs["served_model"] == "served/model-id"
        assert obs["cost_quality"] == "exact"

    def test_unknown_cost_not_coerced_via_or_zero(self) -> None:
        """Negative mutation surface ``unknown-cost-at-adapter-as-zero``.

        ``or 0.0`` on a three-valued cost is the defect class under review.
        """
        cost, _d, _n = parse_provider_cost(None)
        # Explicit three-way branch — not bare truthiness.
        rendered = 0.0 if cost is None else float(cost)
        # The *observation* must stay None; only a deliberate budget default
        # may choose a numeric path after an explicit None check.
        assert cost is None
        assert rendered == 0.0  # deliberate after None-check, not via `or`


# ---------------------------------------------------------------------------
# Billed failure retains cost
# ---------------------------------------------------------------------------


class TestBilledFailure:
    def test_http_402_with_cost_retains_exact_observation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import requests

        class _Resp:
            status_code = 402

            def json(self) -> dict[str, Any]:
                return {
                    "error": {"message": "Payment required", "code": 402},
                    "model": ALLOWED_MODEL,
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 5,
                        "cost": EXACT_COST,
                    },
                }

            def raise_for_status(self) -> None:
                raise requests.HTTPError("402")

        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
        result = _ProbeAdapter().run(_agent_input())
        assert result.status == ResultStatus.ERROR
        assert result.usage.cost_usd == pytest.approx(EXACT_COST)
        obs = _observation_from_result(result)
        assert obs is not None
        assert obs["cost_usd_nanos"] == EXACT_NANOS
        assert obs["served_model"] == ALLOWED_MODEL
        assert obs["provider"] == "openrouter"
        assert obs["cost_quality"] == "exact"

    def test_malformed_body_still_retains_cost_when_usage_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import requests

        class _Resp:
            status_code = 200

            def json(self) -> dict[str, Any]:
                # No choices — malformed success body, but usage+cost present.
                return {
                    "model": ALLOWED_MODEL,
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "cost": EXACT_COST,
                    },
                }

            def raise_for_status(self) -> None:
                return None

        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
        result = _ProbeAdapter().run(_agent_input())
        assert result.status == ResultStatus.ERROR
        assert result.usage.cost_usd == pytest.approx(EXACT_COST)


# ---------------------------------------------------------------------------
# Strict model identity
# ---------------------------------------------------------------------------


class TestStrictModel:
    def test_strict_model_does_not_append_fallback_candidates(self) -> None:
        adapter = _ProbeAdapter()
        models = adapter.candidate_models(
            _agent_input(model=ALLOWED_MODEL, strict=True)
        )
        assert models == [ALLOWED_MODEL]

    def test_non_strict_includes_configured_fallbacks(self) -> None:
        adapter = _ProbeAdapter()
        models = adapter.candidate_models(_agent_input(model=ALLOWED_MODEL))
        assert models[0] == ALLOWED_MODEL
        assert ALLOWED_FALLBACK in models


# ---------------------------------------------------------------------------
# CostObservation builder (common path)
# ---------------------------------------------------------------------------


class TestCostObservationBuilder:
    def test_build_cost_observation_exact(self) -> None:
        inp = _agent_input()
        norm = normalize_provider_cost(EXACT_COST)
        obs = build_cost_observation(
            input=inp,
            normalized=norm,
            provider="openrouter",
            requested_model=ALLOWED_MODEL,
            effective_model=ALLOWED_MODEL,
            transport="http",
            adapter_key="openrouter",
            input_tokens=3,
            output_tokens=5,
        )
        assert isinstance(obs, CostObservation)
        assert obs.cost_quality == CostQuality.EXACT
        assert obs.cost_usd_nanos == EXACT_NANOS
        assert obs.cost_usd_decimal == EXACT_DECIMAL
        wire = obs.model_dump(mode="json")
        again = CostObservation.model_validate(wire)
        assert again.cost_usd_nanos == EXACT_NANOS


# ---------------------------------------------------------------------------
# Post-kill bounded reaping
# ---------------------------------------------------------------------------


class TestPostKillBoundedReap:
    def test_bounded_communicate_never_uses_none_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: bare ``communicate()`` after SIGKILL pins the slot forever.

        Negative mutation ``unbounded-post-kill-communicate`` removes the bound.
        """
        timeouts: list[float | None] = []

        def fake_communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
            timeouts.append(timeout)
            if timeout is None:
                raise AssertionError("unbounded communicate() after SIGKILL is forbidden")
            if timeout >= POST_KILL_REAP_TIMEOUT_SECONDS:
                raise subprocess.TimeoutExpired(cmd="probe", timeout=timeout)
            return "", ""

        monkeypatch.setattr(subprocess.Popen, "communicate", fake_communicate)

        class _FakeProc:
            def communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
                return fake_communicate(self, input=input, timeout=timeout)

        out, err = CliAdapter._bounded_communicate(
            _FakeProc(), POST_KILL_REAP_TIMEOUT_SECONDS
        )  # type: ignore[arg-type]
        assert out == "" and err == ""
        assert timeouts, "communicate was never called"
        assert all(t is not None for t in timeouts), f"unbounded call seen: {timeouts}"
        assert timeouts[0] == pytest.approx(POST_KILL_REAP_TIMEOUT_SECONDS)

    def test_bounded_communicate_returns_quickly_on_hang(self) -> None:
        started = time.monotonic()

        class _HangProc:
            def communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
                if timeout is None:
                    raise AssertionError("unbounded communicate()")
                raise subprocess.TimeoutExpired(cmd="hang", timeout=timeout)

            def kill(self) -> None:
                return None

        out, err = CliAdapter._bounded_communicate(_HangProc(), 0.15)  # type: ignore[arg-type]
        elapsed = time.monotonic() - started
        assert out == "" and err == ""
        assert elapsed < 2.0

    def test_invoke_cleanup_path_uses_bounded_helper(self) -> None:
        """Source-level binding: post-SIGKILL path must call _bounded_communicate."""
        import inspect

        from omniagentos.adapters import common as common_mod

        source = inspect.getsource(common_mod.CliAdapter)
        assert "_bounded_communicate" in source
        assert "POST_KILL_REAP_TIMEOUT_SECONDS" in source
        # Unbounded bare communicate after kill is the defect.
        # Guard: the helper itself may call communicate(timeout=...), which is fine.
        body = inspect.getsource(common_mod.CliAdapter._bounded_communicate)
        assert "timeout=" in body


# ---------------------------------------------------------------------------
# OpenRouter usage payload verification (I-11)
# ---------------------------------------------------------------------------


class TestOpenRouterUsagePayload:
    def test_openrouter_payload_includes_usage_include_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OpenRouter request must include usage:{include:true} for exact cost.

        Issue I-11: OpenRouter returns exact billed cost only when the request
        payload includes usage:{include:true}. The adapter must send it so
        cost_quality stays "exact" in production.
        """
        # F5 (2026-08-12): the spend guard now settles every OpenRouter call, so
        # this test must not touch the production ledger. Session-scoped conftest
        # already pins OMNIAGENTOS_SPEND_DB, but this test is ALSO invoked as a
        # bare method (the F5 meta-repro calls it directly, bypassing fixtures),
        # so pin a scratch ledger in-body too and rebuild the guard singleton.
        import tempfile
        from pathlib import Path

        import requests

        from omniagentos.adapters import spend_guard as _spend_guard

        scratch_spend_db = Path(tempfile.mkdtemp(prefix="or-usage-spend-")) / "spend.sqlite3"
        monkeypatch.setenv("OMNIAGENTOS_SPEND_DB", str(scratch_spend_db))
        monkeypatch.setattr(_spend_guard, "_DEFAULT_GUARD", None)

        captured_payloads: list[dict[str, Any]] = []

        class _Resp:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {
                    "choices": [{"message": {"content": "ok"}}],
                    "model": ALLOWED_MODEL,
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 5,
                        "cost": EXACT_COST,
                    },
                }

        def _post(url: str, **kwargs: Any) -> Any:
            if "json" in kwargs:
                captured_payloads.append(kwargs["json"])
            return _Resp()

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setattr(requests, "post", _post)

        adapter = OpenRouterAdapter()
        input_obj = AgentInput(
            run_id=new_id("run"),
            task_id=new_id("tsk"),
            prompt="test",
            model=ALLOWED_MODEL,
            budget=BudgetSpec(wall_ms_max=30_000),
        )
        adapter.run(input_obj)

        assert captured_payloads, "No request was made"
        payload = captured_payloads[0]
        assert "usage" in payload, "Payload missing usage field"
        assert isinstance(payload["usage"], dict), "usage field must be a dict"
        assert payload["usage"].get("include") is True, (
            "usage field must contain include:True to receive exact cost from OpenRouter"
        )
