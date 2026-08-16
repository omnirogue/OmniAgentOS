from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from tests.support.db_template import make_store


def _registry(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "key": "kimi-priced",
                        "pricing": {
                            "prompt_usd_per_m": 2.0,
                            "completion_usd_per_m": 8.0,
                            "as_of": "2026-08-12T00:00:00Z",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _observation(model: str) -> dict[str, Any]:
    return {
        "call_id": f"call-{model}",
        "request_id": f"request-{model}",
        "execution_id": f"execution-{model}",
        "stage": "worker",
        "provider": "moonshot",
        "transport": "cli",
        "requested_model": model,
        "effective_model": model,
        "model_lineage": "kimi",
        "billing_provider": "moonshot",
        "adapter_key": "estate-kimi-shim",
        "request_state": "sent",
        "provider_outcome": "exit-code-0",
        "input_tokens": 1_000,
        "output_tokens": 500,
        "total_tokens": 1_500,
        "cost_quality": "estimated",
        "cost_upper_bound_usd_nanos": 5_000_000_000,
        "cost_source": "estate-kimi-shim:coarse-flat-estimate-v1:$5.00-unmeasured",
    }


def test_provider_call_write_prices_tokens_or_records_unknown(
    tmp_path: Path, monkeypatch: Any
) -> None:
    registry = tmp_path / "registry.json"
    config = tmp_path / "missing-modelintel-config.yaml"
    _registry(registry)
    monkeypatch.setattr("omniagentos.modelintel.config.registry_path", lambda: registry)
    monkeypatch.setattr("omniagentos.modelintel.config.config_path", lambda: config)

    store = make_store(SqliteStore, str(tmp_path / "cost-truth.sqlite3"))
    try:
        priced = store.record_provider_call(_observation("kimi-priced"))
        unpriced = store.record_provider_call(_observation("kimi-unpriced"))
    finally:
        store.close()

    assert priced["cost_quality"] == "estimated"
    assert priced["cost_upper_bound_usd_nanos"] == 6_000_000
    assert priced["cost_source"] == "modelintel:kimi-priced@2026-08-12T00:00:00Z"
    assert priced["cost_usd_decimal"] is None
    assert priced["cost_usd_nanos"] is None

    assert unpriced["cost_quality"] == "unknown"
    assert unpriced["cost_upper_bound_usd_nanos"] is None
    assert unpriced["cost_source"] == "modelintel:unpriced:moonshot/kimi-unpriced"
    assert "$5.00" not in unpriced["cost_source"]


def test_provider_call_settlement_does_not_reprice_existing_estimate(
    tmp_path: Path, monkeypatch: Any
) -> None:
    registry = tmp_path / "registry.json"
    config = tmp_path / "missing-modelintel-config.yaml"
    _registry(registry)
    monkeypatch.setattr("omniagentos.modelintel.config.registry_path", lambda: registry)
    monkeypatch.setattr("omniagentos.modelintel.config.config_path", lambda: config)

    store = make_store(SqliteStore, str(tmp_path / "cost-truth.sqlite3"))
    try:
        recorded = store.record_provider_call(_observation("kimi-priced"))
        registry.write_text(
            registry.read_text(encoding="utf-8").replace(
                '"prompt_usd_per_m": 2.0', '"prompt_usd_per_m": 200.0'
            ),
            encoding="utf-8",
        )
        settled = store.settle_provider_call(
            recorded["call_id"],
            {"request_state": "sent", "provider_outcome": "completed"},
        )
    finally:
        store.close()

    assert settled["cost_quality"] == recorded["cost_quality"]
    assert settled["cost_upper_bound_usd_nanos"] == recorded["cost_upper_bound_usd_nanos"]
    assert settled["cost_source"] == recorded["cost_source"]


@pytest.mark.parametrize(
    ("missing_side", "missing_value"),
    [("output", None), ("input", "malformed")],
)
def test_provider_call_partial_tokens_are_unpriced(
    tmp_path: Path, monkeypatch: Any, missing_side: str, missing_value: object
) -> None:
    registry = tmp_path / "registry.json"
    config = tmp_path / "missing-modelintel-config.yaml"
    _registry(registry)
    monkeypatch.setattr("omniagentos.modelintel.config.registry_path", lambda: registry)
    monkeypatch.setattr("omniagentos.modelintel.config.config_path", lambda: config)

    observation = _observation("kimi-priced")
    observation[f"{missing_side}_tokens"] = missing_value
    observation["total_tokens"] = None
    store = make_store(SqliteStore, str(tmp_path / "cost-truth.sqlite3"))
    try:
        recorded = store.record_provider_call(observation)
    finally:
        store.close()

    assert recorded["cost_quality"] == "unknown"
    assert recorded["cost_upper_bound_usd_nanos"] is None
    assert recorded["cost_source"] == (
        f"modelintel:unpriced:partial-tokens:missing-{missing_side}"
    )
