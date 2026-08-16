"""P1-COST-EDGE: deterministic localhost OpenRouter contract server.

PC1–PC4, PC7–PC8, PC11–PC13 must be deterministic and loopback-only.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from omniagentos.adapters.api_base import OpenAiCompatibleAdapter, parse_provider_cost
from omniagentos.contracts import AgentInput, BudgetSpec, ResultStatus, new_id
from omniagentos.routing.api_policy import API_PATH_OPENROUTER
from tests.providers.contract_server import (
    DEFAULT_SERVED_MODEL,
    EXACT_COST_DECIMAL,
    EXACT_COST_FLOAT,
    EXACT_COST_NANOS,
    LARGE_COST_DECIMAL,
    LARGE_COST_NANOS,
    PC4_WRONG_MODEL,
    SCENARIO_IDS,
    start_contract_server,
    stop_contract_server,
)


@pytest.fixture
def contract_server():
    server, base_url, state = start_contract_server("127.0.0.1", 0)
    try:
        yield server, base_url, state
    finally:
        stop_contract_server(server)


class _ContractAdapter(OpenAiCompatibleAdapter):
    name = "openrouter"
    api_path = API_PATH_OPENROUTER
    requires_key = False

    def __init__(self, base_url: str, *, scenario: str | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._scenario = scenario

    def api_base(self) -> str:
        return self._base

    def api_key(self) -> str | None:
        return "sk-contract"

    def default_models(self) -> tuple[str, ...]:
        return (DEFAULT_SERVED_MODEL,)

    def extra_headers(self) -> dict[str, str]:
        if self._scenario:
            return {"X-Omni-Contract-Scenario": self._scenario}
        return {}


def _input(model: str = DEFAULT_SERVED_MODEL, *, strict: bool = False) -> AgentInput:
    meta: dict[str, Any] = {}
    if strict:
        meta["strict_model"] = True
    return AgentInput(
        run_id=new_id("run"),
        task_id=new_id("tsk"),
        prompt="contract probe",
        model=model,
        budget=BudgetSpec(wall_ms_max=10_000),
        metadata=meta,
    )


def _post(
    base_url: str, scenario: str, model: str, content: str = "hi"
) -> requests.Response:
    return requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Content-Type": "application/json",
            "X-Omni-Contract-Scenario": scenario,
            "Authorization": "Bearer sk-contract",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": content}],
        },
        timeout=5.0,
    )


def _obs_from_result(result) -> dict[str, Any] | None:
    for receipt in result.receipts or []:
        if receipt.key == "cost_observation_json" and receipt.target:
            return json.loads(receipt.target)
    return None


class TestContractServerBinding:
    def test_binds_loopback_only(self, contract_server) -> None:
        server, base_url, _state = contract_server
        host, _port = server.server_address[:2]
        assert host in {"127.0.0.1", "localhost", "::1"}
        assert base_url.startswith("http://127.0.0.1:") or base_url.startswith(
            "http://localhost:"
        )

    def test_rejects_non_loopback_bind(self) -> None:
        with pytest.raises(ValueError, match="loopback"):
            start_contract_server("0.0.0.0", 0)

    def test_scenario_catalog_covers_required_ids(self) -> None:
        required = {"PC1", "PC2", "PC3", "PC4", "PC7", "PC8", "PC11", "PC12", "PC13"}
        assert required.issubset(set(SCENARIO_IDS))


class TestPCScenarios:
    def test_pc1_exact_cost_success(self, contract_server) -> None:
        _server, base_url, state = contract_server
        resp = _post(base_url, "PC1", DEFAULT_SERVED_MODEL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["model"] == DEFAULT_SERVED_MODEL
        # Server preserves decimal text for exact nano identity.
        raw_cost = body["usage"]["cost"]
        assert str(raw_cost) == EXACT_COST_DECIMAL or raw_cost == pytest.approx(
            EXACT_COST_FLOAT
        )
        cost, decimal_text, nanos = parse_provider_cost(raw_cost)
        assert decimal_text == EXACT_COST_DECIMAL
        assert nanos == EXACT_COST_NANOS
        assert cost == pytest.approx(EXACT_COST_FLOAT)
        assert state.requests[-1]["scenario"] == "PC1"

    def test_pc2_missing_cost_is_unknown_at_adapter(self, contract_server) -> None:
        _server, base_url, _state = contract_server
        adapter = _ContractAdapter(base_url, scenario="PC2")
        result = adapter.run(_input())
        assert result.status == ResultStatus.OK
        assert result.usage.cost_usd is None
        obs = _obs_from_result(result)
        assert obs is not None
        assert obs["cost_quality"] == "unknown"
        assert obs["cost_usd_nanos"] is None

    def test_pc3_billed_failure_retains_cost_through_adapter(
        self, contract_server
    ) -> None:
        _server, base_url, _state = contract_server
        adapter = _ContractAdapter(base_url, scenario="PC3")
        result = adapter.run(_input())
        assert result.status == ResultStatus.ERROR
        assert result.usage.cost_usd == pytest.approx(EXACT_COST_FLOAT)
        obs = _obs_from_result(result)
        assert obs is not None
        assert obs["cost_usd_nanos"] == EXACT_COST_NANOS
        assert obs["cost_quality"] == "exact"
        assert obs["served_model"] == DEFAULT_SERVED_MODEL
        assert obs["provider"] == "openrouter"

    def test_pc4_strict_served_model_echo(self, contract_server) -> None:
        _server, base_url, state = contract_server
        assert state.accept_wrong_model is False
        model = DEFAULT_SERVED_MODEL
        resp = _post(base_url, "PC4", model)
        assert resp.status_code == 200
        assert resp.json()["model"] == model

    def test_pc4_does_not_silently_swap_model_by_default(self, contract_server) -> None:
        """Negative-mutation surface: accept_wrong_model must stay False."""
        _server, base_url, state = contract_server
        assert state.accept_wrong_model is False
        model = DEFAULT_SERVED_MODEL
        resp = _post(base_url, "PC4", model)
        served = resp.json()["model"]
        assert served == model
        assert served != PC4_WRONG_MODEL

    def test_pc4_wrong_model_mutation_flag_is_detectable(self, contract_server) -> None:
        """When the mutation flag is on, the server swaps identity — tests must catch it."""
        _server, base_url, state = contract_server
        state.accept_wrong_model = True
        model = DEFAULT_SERVED_MODEL
        resp = _post(base_url, "PC4", model)
        assert resp.json()["model"] == PC4_WRONG_MODEL
        # Production default is False; the lane asserts the default path above.

    def test_pc4_adapter_strict_model_rejects_swap(self, contract_server) -> None:
        _server, base_url, state = contract_server
        state.accept_wrong_model = True
        adapter = _ContractAdapter(base_url, scenario="PC4")
        result = adapter.run(_input(strict=True))
        assert result.status == ResultStatus.ERROR
        assert "strict model mismatch" in (result.error or "").lower()

    def test_pc7_delayed_response_is_bounded(self, contract_server) -> None:
        _server, base_url, _state = contract_server
        with pytest.raises(requests.Timeout):
            requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "X-Omni-Contract-Scenario": "PC7",
                },
                json={
                    "model": DEFAULT_SERVED_MODEL,
                    "messages": [{"role": "user", "content": "slow"}],
                },
                timeout=0.3,
            )

    def test_pc8_first_503_then_success(self, contract_server) -> None:
        _server, base_url, state = contract_server
        r1 = _post(base_url, "PC8", DEFAULT_SERVED_MODEL)
        assert r1.status_code == 503
        r2 = _post(base_url, "PC8", DEFAULT_SERVED_MODEL)
        assert r2.status_code == 200
        raw_cost = r2.json()["usage"]["cost"]
        cost, decimal_text, nanos = parse_provider_cost(raw_cost)
        assert decimal_text == EXACT_COST_DECIMAL
        assert nanos == EXACT_COST_NANOS
        assert cost == pytest.approx(EXACT_COST_FLOAT)
        assert state.pc8_hits == 2

    def test_pc11_exact_zero_is_preserved(self, contract_server) -> None:
        _server, base_url, _state = contract_server
        resp = _post(base_url, "PC11", DEFAULT_SERVED_MODEL)
        body = resp.json()
        cost, decimal_text, nanos = parse_provider_cost(body["usage"]["cost"])
        assert cost == 0.0
        assert nanos == 0
        assert decimal_text in {"0", "0.0"}

    def test_pc12_invalid_cost_becomes_unknown_at_adapter(self, contract_server) -> None:
        _server, base_url, _state = contract_server
        adapter = _ContractAdapter(base_url, scenario="PC12")
        result = adapter.run(_input())
        assert result.status == ResultStatus.OK
        assert result.usage.cost_usd is None
        obs = _obs_from_result(result)
        assert obs is not None
        assert obs["cost_quality"] == "unknown"

    def test_pc13_large_cost_nano_roundtrip(self, contract_server) -> None:
        _server, base_url, _state = contract_server
        resp = _post(base_url, "PC13", DEFAULT_SERVED_MODEL)
        body = resp.json()
        cost, decimal_text, nanos = parse_provider_cost(body["usage"]["cost"])
        assert decimal_text == LARGE_COST_DECIMAL
        assert nanos == LARGE_COST_NANOS
        assert cost == pytest.approx(999.000000001)

    def test_adapter_pc1_end_to_end_observation(self, contract_server) -> None:
        _server, base_url, _state = contract_server
        adapter = _ContractAdapter(base_url, scenario="PC1")
        result = adapter.run(_input())
        assert result.status == ResultStatus.OK
        assert result.usage.cost_usd == pytest.approx(EXACT_COST_FLOAT)
        obs = _obs_from_result(result)
        assert obs is not None
        assert obs["cost_usd_decimal"] == EXACT_COST_DECIMAL
        assert obs["cost_usd_nanos"] == EXACT_COST_NANOS
        assert obs["provider"] == "openrouter"
