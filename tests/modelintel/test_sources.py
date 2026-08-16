"""Offline tests for the deterministic benchmark/pricing fetchers — every test
mocks `requests.get` with RECORDED-shape fixtures; nothing here touches the
live network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests

from omniagentos.modelintel import sources as sources_mod
from omniagentos.modelintel.config import ModelIntelConfig, ModelSpec


class _FakeResponse:
    def __init__(self, status: int = 200, json_data: Any = None, text: str | None = None) -> None:
        self.status_code = status
        self._json = json_data
        self.text = text if text is not None else ""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._json


@pytest.fixture(autouse=True)
def _isolate_raw_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # _cache_raw() writes under var_dir() — never let a test touch the real repo.
    monkeypatch.setenv("OMNIAGENTOS_MODELINTEL_DIR", str(tmp_path))


def _cfg() -> ModelIntelConfig:
    return ModelIntelConfig(
        models=[
            ModelSpec(
                key="alpha",
                title="Alpha",
                provider="p",
                lineage="l",
                aliases=["vendor/alpha", "Alpha Model"],
            ),
            ModelSpec(key="beta", title="Beta", provider="p", lineage="l", aliases=["beta-model"]),
        ]
    )


# --- Artificial Analysis ----------------------------------------------------


def test_fetch_aa_skips_network_when_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources_mod, "aa_api_key", lambda: None)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not attempt HTTP call without AA_API_KEY")

    monkeypatch.setattr(sources_mod.requests, "get", _boom)
    result = sources_mod.fetch_artificial_analysis(_cfg())
    assert result.ok is False
    assert result.error == "AA_API_KEY not set"
    assert result.rows == []
    assert result.facts == []


def test_fetch_aa_parses_quality_speed_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources_mod, "aa_api_key", lambda: "test-key")
    payload = {
        "status": 200,
        "data": [
            {
                "id": "m1",
                "name": "vendor/alpha",
                "slug": "alpha",
                "model_creator": {"slug": "p", "name": "P"},
                "evaluations": {
                    "artificial_analysis_intelligence_index": 60.0,
                    "artificial_analysis_coding_index": 71.5,
                },
                "pricing": {"price_1m_input_tokens": 3.0, "price_1m_output_tokens": 15.0},
                "performance": {
                    "median_output_tokens_per_second": 78.3,
                    "median_time_to_first_token_seconds": 0.62,
                },
            },
            {
                # no coding index published -> NO aa-coding-index row (never
                # silently substitutes the unrelated intelligence index)
                "id": "m2",
                "name": "beta-model",
                "evaluations": {"artificial_analysis_intelligence_index": 55.0},
                "pricing": {"price_1m_input_tokens": 1.0, "price_1m_output_tokens": 2.0},
            },
            {
                # unusable entry: no name/slug at all -> skipped, not a crash
                "id": "m3",
                "evaluations": {},
            },
        ],
        "pagination": {"current_page": 1, "total_pages": 1, "has_more": False},
    }

    captured: dict[str, Any] = {}

    def _get(
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int = 0,
    ) -> _FakeResponse:
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        return _FakeResponse(json_data=payload, text="raw")

    monkeypatch.setattr(sources_mod.requests, "get", _get)
    result = sources_mod.fetch_artificial_analysis(_cfg())

    assert result.ok is True
    assert captured["headers"] == {"x-api-key": "test-key"}
    assert captured["url"] == sources_mod.ARTIFICIAL_ANALYSIS_URL

    rows_by_model = {r.model_name: r for r in result.rows}
    assert rows_by_model["vendor/alpha"].score == 71.5
    assert rows_by_model["vendor/alpha"].benchmark == "aa-coding-index"
    assert "coding index" in (rows_by_model["vendor/alpha"].note or "")
    assert "beta-model" not in rows_by_model  # no coding index -> no row emitted
    assert len(result.rows) == 1  # m2 has no coding index, m3 has no usable name

    facts_by_model = {f.model_name: f for f in result.facts}
    alpha_facts = facts_by_model["vendor/alpha"]
    assert alpha_facts.prompt_usd_per_m == 3.0
    assert alpha_facts.completion_usd_per_m == 15.0
    assert alpha_facts.tokens_per_second == 78.3  # parsed from entry["performance"]
    assert alpha_facts.ttft_seconds == 0.62
    beta_facts = facts_by_model["beta-model"]
    assert beta_facts.prompt_usd_per_m == 1.0  # pricing/facts still recorded
    assert beta_facts.tokens_per_second is None  # no performance block published
    # m3 has no name, so it never reaches the facts list either
    assert len(result.facts) == 2


def test_fetch_aa_reads_performance_block_not_top_level_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: AA nests throughput/TTFT under entry["performance"], not at
    the top level — a flattened fixture would mask this."""
    monkeypatch.setattr(sources_mod, "aa_api_key", lambda: "test-key")
    payload = {
        "data": [
            {
                "name": "vendor/alpha",
                "evaluations": {"artificial_analysis_coding_index": 71.5},
                "pricing": {},
                # top-level fields (wrong location) must be IGNORED
                "median_output_tokens_per_second": 999.0,
                "median_time_to_first_token_seconds": 999.0,
                "performance": {
                    "median_output_tokens_per_second": 42.0,
                    "median_time_to_first_token_seconds": 0.5,
                },
            }
        ],
        "pagination": {"has_more": False},
    }
    monkeypatch.setattr(
        sources_mod.requests, "get", lambda *a, **k: _FakeResponse(json_data=payload, text="raw")
    )
    result = sources_mod.fetch_artificial_analysis(_cfg())
    assert result.ok is True
    fact = result.facts[0]
    assert fact.tokens_per_second == 42.0
    assert fact.ttft_seconds == 0.5


def test_fetch_aa_follows_pagination_across_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources_mod, "aa_api_key", lambda: "test-key")
    page1 = {
        "data": [
            {
                "name": "vendor/alpha",
                "evaluations": {"artificial_analysis_coding_index": 50.0},
                "pricing": {},
            }
        ],
        "pagination": {"current_page": 1, "total_pages": 2, "has_more": True},
    }
    page2 = {
        "data": [
            {
                "name": "beta-model",
                "evaluations": {"artificial_analysis_coding_index": 65.0},
                "pricing": {},
            }
        ],
        "pagination": {"current_page": 2, "total_pages": 2, "has_more": False},
    }
    calls: list[int] = []

    def _get(
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: int = 0,
    ) -> _FakeResponse:
        page = (params or {}).get("page", 1)
        calls.append(page)
        return _FakeResponse(json_data=page1 if page == 1 else page2, text="raw")

    monkeypatch.setattr(sources_mod.requests, "get", _get)
    result = sources_mod.fetch_artificial_analysis(_cfg())

    assert result.ok is True
    assert calls == [1, 2]
    names = {r.model_name for r in result.rows}
    # beta-model only exists on page 2 — must not be silently dropped
    assert names == {"vendor/alpha", "beta-model"}


def test_fetch_aa_degrades_when_key_resolver_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> str | None:
        raise PermissionError("cannot read connections.env")

    monkeypatch.setattr(sources_mod, "aa_api_key", _boom)

    def _fail_get(*a: Any, **k: Any) -> Any:
        raise AssertionError("must not attempt an HTTP call when the key resolver fails")

    monkeypatch.setattr(sources_mod.requests, "get", _fail_get)
    result = sources_mod.fetch_artificial_analysis(_cfg())
    assert result.ok is False
    assert result.error and "connections.env" in result.error
    assert result.rows == []
    assert result.facts == []


def test_fetch_all_continues_to_swebench_live_when_aa_resolver_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> str | None:
        raise PermissionError("boom")

    monkeypatch.setattr(sources_mod, "aa_api_key", _boom)
    monkeypatch.setattr(
        sources_mod,
        "fetch_aider",
        lambda: sources_mod.FetchResult(source="aider-polyglot", url="u", fetched_at="t", ok=True),
    )
    monkeypatch.setattr(
        sources_mod,
        "fetch_openrouter",
        lambda cfg: sources_mod.FetchResult(
            source="openrouter-pricing", url="u", fetched_at="t", ok=True
        ),
    )
    monkeypatch.setattr(
        sources_mod,
        "fetch_swebench_live",
        lambda: sources_mod.FetchResult(source="swebench-live", url="u", fetched_at="t", ok=True),
    )
    result = sources_mod.fetch_all(_cfg())
    assert result["aa-coding-index"].ok is False
    assert result["swebench-live"].ok is True  # the sweep continued past AA


def test_fetch_aa_degrades_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources_mod, "aa_api_key", lambda: "test-key")
    monkeypatch.setattr(sources_mod.requests, "get", lambda *a, **k: _FakeResponse(status=401))
    result = sources_mod.fetch_artificial_analysis(_cfg())
    assert result.ok is False
    assert result.error
    assert result.rows == []


# --- SWE-bench-Live ----------------------------------------------------------


def _jsonl(*lines: str) -> str:
    return "\n".join(lines)


def test_fetch_swebench_live_filters_to_lite_split(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _jsonl(
        '{"name": "AMI Agent + Claude-4.6-Opus", "set": "lite", "total": 300, '
        '"resolved": 189, "date": "2026-06-23", "url": "https://x/1"}',
        '{"name": "Slingshot + GPT-5.5 (Medium)", "set": "java", "total": 109, '
        '"resolved": 40, "date": "2026-07-07", "url": "https://x/2"}',
        '{"name": "SomeAgent + Grok-4.5", "set": "lite", "total": 300, '
        '"resolved": [1, 2, 3], "date": "2026-07-01", "url": "https://x/3"}',
        "not json at all",
        '{"set": "lite", "total": 10, "resolved": 5}',  # no "+" in name -> skipped
    )
    monkeypatch.setattr(sources_mod.requests, "get", lambda *a, **k: _FakeResponse(text=text))
    result = sources_mod.fetch_swebench_live()

    assert result.ok is True
    assert len(result.rows) == 2  # java split and the malformed/no-name lines are excluded
    by_model = {r.model_name: r for r in result.rows}
    assert by_model["Claude-4.6-Opus"].score == pytest.approx(63.0)  # 189/300*100
    assert by_model["Claude-4.6-Opus"].benchmark == "swebench-live"
    assert by_model["Grok-4.5"].score == pytest.approx(1.0)  # resolved-as-list: 3/300*100
    assert "resolved=3/300" in (by_model["Grok-4.5"].note or "")


def test_fetch_swebench_live_degrades_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources_mod.requests, "get", lambda *a, **k: _FakeResponse(status=500))
    result = sources_mod.fetch_swebench_live()
    assert result.ok is False
    assert result.rows == []


# --- OpenRouter endpoint-latency cross-check ---------------------------------


def test_percentile_value_accepts_bare_number_and_dict() -> None:
    assert sources_mod._percentile_value(42) == 42.0
    assert sources_mod._percentile_value({"p50": 12.5}) == 12.5
    assert sources_mod._percentile_value({"median": 9}) == 9.0
    assert sources_mod._percentile_value(None) is None
    assert sources_mod._percentile_value({"unrelated": 1}) is None


def test_endpoint_latency_ms_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a: Any, **k: Any) -> Any:
        raise requests.ConnectionError("down")

    monkeypatch.setattr(sources_mod.requests, "get", _boom)
    assert sources_mod._endpoint_latency_ms("vendor/alpha") is None


def test_fetch_openrouter_attaches_pricing_and_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = {
        "data": [
            {
                "id": "vendor/alpha",
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                "context_length": 200_000,
            },
            {"id": "untracked/model", "pricing": {"prompt": "0.000001"}},
        ]
    }
    # OpenRouter publishes latency_last_30m in SECONDS (e.g. 0.38s) — the
    # fixture must reflect that, not pre-converted milliseconds, so the
    # seconds->ms conversion in _endpoint_latency_ms() is actually exercised.
    endpoints = {
        "data": {
            "endpoints": [
                {"latency_last_30m": {"p50": 0.85}},
                {"latency_last_30m": None},
                {"latency_last_30m": 0.9},
            ]
        }
    }

    def _get(url: str, timeout: int = 0) -> _FakeResponse:
        if url == sources_mod.OPENROUTER_MODELS_URL:
            return _FakeResponse(json_data=catalog, text="raw")
        assert url == sources_mod.OPENROUTER_ENDPOINTS_URL.format(model_id="vendor/alpha")
        return _FakeResponse(json_data=endpoints)

    monkeypatch.setattr(sources_mod.requests, "get", _get)
    result = sources_mod.fetch_openrouter(_cfg())

    assert result.ok is True
    assert len(result.facts) == 1  # untracked/model has no alias match
    fact = result.facts[0]
    assert fact.prompt_usd_per_m == 3.0
    assert fact.completion_usd_per_m == 15.0
    # min of the two numeric samples (0.85s), converted to milliseconds
    assert fact.latency_ms_p50 == pytest.approx(850.0)


# --- wiring -------------------------------------------------------------


def test_fetch_all_includes_every_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sources_mod,
        "fetch_aider",
        lambda: sources_mod.FetchResult(source="aider-polyglot", url="u", fetched_at="t", ok=True),
    )
    monkeypatch.setattr(
        sources_mod,
        "fetch_openrouter",
        lambda cfg: sources_mod.FetchResult(
            source="openrouter-pricing", url="u", fetched_at="t", ok=True
        ),
    )
    monkeypatch.setattr(
        sources_mod,
        "fetch_artificial_analysis",
        lambda cfg: sources_mod.FetchResult(
            source="aa-coding-index", url="u", fetched_at="t", ok=False, error="AA_API_KEY not set"
        ),
    )
    monkeypatch.setattr(
        sources_mod,
        "fetch_swebench_live",
        lambda: sources_mod.FetchResult(source="swebench-live", url="u", fetched_at="t", ok=True),
    )
    result = sources_mod.fetch_all(_cfg())
    assert set(result.keys()) == {
        "aider-polyglot",
        "openrouter-pricing",
        "aa-coding-index",
        "swebench-live",
    }
