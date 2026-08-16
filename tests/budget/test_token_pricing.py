"""Pricing measured tokens must produce an estimate or nothing — never a guess.

C4. ``omniagentos.budget.token_pricing`` is the only place a dollar figure is
derived from token counts. Every path that cannot cite a published rate has to
return ``None`` so the caller persists SQL NULL: an unpriceable session stays
unpriced instead of acquiring an invented number.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.budget.token_pricing import (
    QUALITY_ESTIMATED,
    QUALITY_EXACT,
    QUALITY_UNKNOWN,
    cost_quality,
    estimate_token_cost,
)

# Rates mirror the live registry's shape exactly (see var/modelintel/registry.json).
_REGISTRY: dict[str, Any] = {
    "schema_version": 1,
    "updated_at": "2026-08-04T11:15:05Z",
    "models": [
        {
            "key": "gpt-5.6-sol",
            "pricing": {
                "prompt_usd_per_m": 5.0,
                "completion_usd_per_m": 30.0,
                "as_of": "2026-08-04T11:15:05Z",
                "source": "openrouter",
            },
        },
        {"key": "model-without-pricing"},
        {"key": "model-half-priced", "pricing": {"prompt_usd_per_m": 5.0}},
        {
            "key": "model-genuinely-free",
            "pricing": {"prompt_usd_per_m": 0.0, "completion_usd_per_m": 0.0},
        },
    ],
}


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(_REGISTRY), encoding="utf-8")
    return path


def test_published_rates_price_measured_tokens(registry: Path) -> None:
    estimate = estimate_token_cost("gpt-5.6-sol", 18_000, 3_000, registry_file=registry)

    assert estimate is not None
    # (18_000 * $5 + 3_000 * $30) / 1e6
    assert estimate.cost_usd == pytest.approx(0.18)
    assert estimate.model_key == "gpt-5.6-sol"
    assert estimate.source == "modelintel:gpt-5.6-sol@2026-08-04T11:15:05Z"


def test_cache_write_tokens_bill_at_the_prompt_rate_as_an_upper_bound(registry: Path) -> None:
    """No cache rate is published, so the estimate deliberately over-counts."""
    plain = estimate_token_cost("gpt-5.6-sol", 1_000, 0, registry_file=registry)
    cached = estimate_token_cost(
        "gpt-5.6-sol", 1_000, 0, cache_write_input_tokens=1_000, registry_file=registry
    )

    assert plain is not None and cached is not None
    assert cached.cost_usd > plain.cost_usd
    assert cached.cost_usd == pytest.approx(0.01)


def test_config_suffix_resolves_to_the_registry_key(registry: Path) -> None:
    """ "gpt-5.6-sol (high)" is a CONFIG of a model, not an unlisted model."""
    estimate = estimate_token_cost("GPT-5.6 Sol (high)", 1_000_000, 0, registry_file=registry)

    assert estimate is not None
    assert estimate.model_key == "gpt-5.6-sol"
    assert estimate.cost_usd == pytest.approx(5.0)


@pytest.mark.parametrize(
    "model",
    ["model-not-in-registry", "", "   "],
)
def test_unlisted_or_blank_model_is_unpriceable(registry: Path, model: str) -> None:
    assert estimate_token_cost(model, 10_000, 1_000, registry_file=registry) is None


def test_missing_model_name_is_unpriceable(registry: Path) -> None:
    assert estimate_token_cost(None, 10_000, 1_000, registry_file=registry) is None


def test_listed_model_without_published_rates_is_unpriceable(registry: Path) -> None:
    assert (
        estimate_token_cost("model-without-pricing", 10_000, 1_000, registry_file=registry) is None
    )


def test_half_a_price_table_is_unpriceable_rather_than_substituted(registry: Path) -> None:
    """Filling in the missing half would be inventing, so stay unknown."""
    assert estimate_token_cost("model-half-priced", 10_000, 1_000, registry_file=registry) is None


def test_a_published_zero_rate_is_a_real_price_not_a_missing_one(registry: Path) -> None:
    """$0/M is data. The estimate is 0.0 — and it is an estimate, not a NULL."""
    estimate = estimate_token_cost("model-genuinely-free", 10_000, 1_000, registry_file=registry)

    assert estimate is not None
    assert estimate.cost_usd == 0.0


def test_missing_registry_is_unpriceable(tmp_path: Path) -> None:
    assert (
        estimate_token_cost("gpt-5.6-sol", 10_000, 1_000, registry_file=tmp_path / "gone") is None
    )


def test_unreadable_registry_is_unpriceable_and_does_not_raise(tmp_path: Path) -> None:
    broken = tmp_path / "registry.json"
    broken.write_text("{not json", encoding="utf-8")

    assert estimate_token_cost("gpt-5.6-sol", 10_000, 1_000, registry_file=broken) is None


def test_absent_token_counts_are_unpriceable(registry: Path) -> None:
    assert estimate_token_cost("gpt-5.6-sol", None, None, registry_file=registry) is None


def test_bool_token_counts_are_rejected_not_coerced_to_one(registry: Path) -> None:
    """bool is an int subclass; a stray True must never price as 1 token."""
    assert estimate_token_cost("gpt-5.6-sol", True, True, registry_file=registry) is None


def test_negative_token_counts_are_rejected(registry: Path) -> None:
    estimate = estimate_token_cost("gpt-5.6-sol", -5, 1_000, registry_file=registry)

    # The negative input is dropped, the valid output half still prices.
    assert estimate is not None
    assert estimate.cost_usd == pytest.approx(0.03)


def test_rewritten_registry_invalidates_the_cache(tmp_path: Path) -> None:
    """The updater rewrites registry.json daily; a stale price must not stick."""
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(_REGISTRY), encoding="utf-8")
    first = estimate_token_cost("gpt-5.6-sol", 1_000_000, 0, registry_file=path)

    doubled = json.loads(json.dumps(_REGISTRY))
    doubled["models"][0]["pricing"]["prompt_usd_per_m"] = 10.0
    path.write_text(json.dumps(doubled), encoding="utf-8")
    # Rewrite changes size and mtime_ns; the cache key is (mtime_ns, size).
    second = estimate_token_cost("gpt-5.6-sol", 1_000_000, 0, registry_file=path)

    assert first is not None and second is not None
    assert first.cost_usd == pytest.approx(5.0)
    assert second.cost_usd == pytest.approx(10.0)


def test_cost_quality_is_derived_and_exact_outranks_estimated() -> None:
    """The discriminator cannot contradict the numbers it describes."""
    assert cost_quality(None, None) == QUALITY_UNKNOWN
    assert cost_quality(None, 0.18) == QUALITY_ESTIMATED
    assert cost_quality(1.25, None) == QUALITY_EXACT
    # A genuine free run is EXACT, never "estimated" and never "unknown".
    assert cost_quality(0.0, None) == QUALITY_EXACT
    assert cost_quality(0.0, 0.18) == QUALITY_EXACT
