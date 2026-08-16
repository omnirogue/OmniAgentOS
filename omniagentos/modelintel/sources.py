"""Deterministic benchmark/pricing fetchers (no LLM involved).

Each fetcher is best-effort: on any failure it returns FetchResult(ok=False)
and the registry keeps last-known-good values — a broken leaderboard must
never take down the daily update. Raw payloads are cached under
var/modelintel/raw/<date>/ so a bad merge can always be re-derived.

Sources (fetcher key == FetchResult.source, matching the codebase convention
that a single-benchmark fetcher's source name equals the benchmark key):
- aider-polyglot: Aider polyglot leaderboard (percent, diff-edit correctness).
- openrouter-pricing: OpenRouter /api/v1/models catalog — live pricing/context
  for TRACKED models, plus a best-effort per-model endpoint-latency cross-check
  (/api/v1/models/{id}/endpoints; often null until a model has real traffic —
  that's fine, it's a nullable cross-check field, never load-bearing).
- aa-coding-index: Artificial Analysis /api/v2/language/models (x-api-key,
  AA_API_KEY) — coding-quality index plus tokens/sec, time-to-first-token, and
  $/Mtok pricing as a second, independently-measured cross-check on both
  quality and cost. Skips the network call entirely (ok=False, no HTTP
  attempt) when AA_API_KEY is unset — this is a paid API and must degrade
  silently, not spam a 401.
- swebench-live: SWE-bench-Live "lite" split leaderboard (continuously
  refreshed, real submissions) — a deterministic cross-check/override
  alongside the Grok-research-sourced SWE-bench Verified numbers.
"""

from __future__ import annotations

import json
from typing import Any

import requests
import yaml
from pydantic import BaseModel, Field

from omniagentos.contracts import utc_now_iso
from omniagentos.modelintel.config import (
    ModelIntelConfig,
    aa_api_key,
    build_alias_index,
    normalize_model_name,
    var_dir,
)

AIDER_LEADERBOARD_URL = (
    "https://raw.githubusercontent.com/Aider-AI/aider/main/"
    "aider/website/_data/polyglot_leaderboard.yml"
)
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model_id}/endpoints"
ARTIFICIAL_ANALYSIS_URL = "https://artificialanalysis.ai/api/v2/language/models"
# The upstream leaderboard site embeds a date-stamped filename directly in its
# JS (no stable/versionless alias exists as of this writing) — see
# https://swe-bench-live.github.io/leaderboard.js. When this 404s the fetcher
# degrades to ok=False like any other broken source; last-known-good rows
# carry forward until the URL below is bumped to the current filename.
SWEBENCH_LIVE_URL = "https://swe-bench-live.github.io/reports-0605.jsonl"
SWEBENCH_LIVE_SET = "lite"
HTTP_TIMEOUT = 30
# AA paginates /api/v2/language/models (`pagination.has_more`/`total_pages`);
# this is a hard safety cap on how many pages one fetch will ever follow, so a
# misbehaving/looping API can never hang the daily update.
AA_MAX_PAGES = 20


class BenchmarkRow(BaseModel):
    benchmark: str
    model_name: str  # name exactly as published by the source
    score: float | None = None
    metric: str = "percent"
    source_url: str
    note: str | None = None
    # Provenance the registry stamps onto WATCHLIST rows (untracked models) so
    # a future day can carry a row forward source-by-source when its specific
    # origin source fails, instead of all-or-nothing on the whole sweep.
    # Fetchers/research never need to set these; registry.build() does.
    source: str = ""
    as_of: str = ""


class ModelFacts(BaseModel):
    model_name: str  # name/id exactly as published by the source
    prompt_usd_per_m: float | None = None
    completion_usd_per_m: float | None = None
    context_length: int | None = None
    tokens_per_second: float | None = None  # live throughput cross-check
    ttft_seconds: float | None = None  # time-to-first-token cross-check
    latency_ms_p50: float | None = None  # OpenRouter endpoint latency cross-check


class FetchResult(BaseModel):
    source: str
    url: str
    fetched_at: str
    ok: bool
    error: str | None = None
    rows: list[BenchmarkRow] = Field(default_factory=list)
    facts: list[ModelFacts] = Field(default_factory=list)


def _cache_raw(source: str, payload: str, suffix: str) -> None:
    day = utc_now_iso()[:10]
    target = var_dir() / "raw" / day / f"{source}.{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")


def fetch_aider() -> FetchResult:
    """Aider polyglot leaderboard — headline pass_rate_2 percent per model."""
    fetched_at = utc_now_iso()
    try:
        resp = requests.get(AIDER_LEADERBOARD_URL, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        _cache_raw("aider-polyglot", resp.text, "yml")
        entries = yaml.safe_load(resp.text) or []
        rows = [
            BenchmarkRow(
                benchmark="aider-polyglot",
                model_name=str(entry.get("model", "")),
                score=float(entry["pass_rate_2"]),
                metric="percent",
                source_url="https://aider.chat/docs/leaderboards/",
                note=f"edit_format={entry.get('edit_format')}",
            )
            for entry in entries
            if entry.get("model") and entry.get("pass_rate_2") is not None
        ]
        return FetchResult(
            source="aider-polyglot",
            url=AIDER_LEADERBOARD_URL,
            fetched_at=fetched_at,
            ok=True,
            rows=rows,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort by design
        return FetchResult(
            source="aider-polyglot",
            url=AIDER_LEADERBOARD_URL,
            fetched_at=fetched_at,
            ok=False,
            error=str(exc),
        )


def _coerce_float(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _usd_per_million(raw: Any) -> float | None:
    value = _coerce_float(raw)
    return None if value is None else round(value * 1_000_000, 4)


def _percentile_value(raw: Any) -> float | None:
    """OpenRouter's `latency_last_30m`/`throughput_last_30m` fields have
    shipped as either a bare number or a `{"p50": ...}`-shaped object; accept
    both defensively since this is a purely informational cross-check."""
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, dict):
        for key in ("p50", "median", "mean", "p90"):
            value = _coerce_float(raw.get(key))
            if value is not None:
                return value
    return None


def _endpoint_latency_ms(model_id: str) -> float | None:
    """Best-effort per-model latency cross-check via OpenRouter's endpoints
    API. Sparsely populated (often null) — never load-bearing, and a failed
    probe for one model must never fail the whole pricing fetch.

    `latency_last_30m` is published in SECONDS (e.g. 0.38); the field this
    returns is named/used as milliseconds throughout the codebase
    (`latency_ms_p50`, alongside `measured_latency_ms`/`warmLatencyMs`), so the
    raw seconds value is converted to ms here — the ONLY place that happens."""
    try:
        resp = requests.get(
            OPENROUTER_ENDPOINTS_URL.format(model_id=model_id), timeout=HTTP_TIMEOUT
        )
        resp.raise_for_status()
        endpoints = (resp.json().get("data") or {}).get("endpoints") or []
        samples = [
            v
            for ep in endpoints
            if (v := _percentile_value(ep.get("latency_last_30m"))) is not None
        ]
        return min(samples) * 1000 if samples else None
    except Exception:  # noqa: BLE001 - purely informational cross-check
        return None


def fetch_openrouter(cfg: ModelIntelConfig) -> FetchResult:
    """OpenRouter model catalog — live pricing + context for TRACKED models only
    (the full catalog is ~350 models; we keep just alias matches), plus a
    best-effort per-model endpoint-latency cross-check."""
    fetched_at = utc_now_iso()
    try:
        resp = requests.get(OPENROUTER_MODELS_URL, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        _cache_raw("openrouter", resp.text, "json")
        catalog = resp.json().get("data", [])
        alias_index = build_alias_index(cfg)
        facts = []
        for item in catalog:
            model_id = str(item.get("id", ""))
            if normalize_model_name(model_id) not in alias_index:
                continue
            pricing = item.get("pricing") or {}
            facts.append(
                ModelFacts(
                    model_name=model_id,
                    prompt_usd_per_m=_usd_per_million(pricing.get("prompt")),
                    completion_usd_per_m=_usd_per_million(pricing.get("completion")),
                    context_length=item.get("context_length"),
                    latency_ms_p50=_endpoint_latency_ms(model_id),
                )
            )
        return FetchResult(
            source="openrouter-pricing",
            url=OPENROUTER_MODELS_URL,
            fetched_at=fetched_at,
            ok=True,
            facts=facts,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort by design
        return FetchResult(
            source="openrouter-pricing",
            url=OPENROUTER_MODELS_URL,
            fetched_at=fetched_at,
            ok=False,
            error=str(exc),
        )


def fetch_artificial_analysis(cfg: ModelIntelConfig) -> FetchResult:
    """Artificial Analysis /api/v2/language/models — coding-quality index plus
    tokens/sec, time-to-first-token, and $/Mtok pricing, all independently
    measured (not vendor-reported). Requires AA_API_KEY (paid API): with no
    key set this returns ok=False WITHOUT attempting the HTTP call — a missing
    key is an intentionally-off cross-check, not a broken source. The response
    is paginated (`pagination.has_more`/`total_pages`); every page is followed
    (bounded by AA_MAX_PAGES) so models on later pages are never silently
    dropped."""
    fetched_at = utc_now_iso()
    try:
        api_key = aa_api_key()
    except Exception as exc:  # noqa: BLE001 - a broken credential resolver (e.g.
        # an unreadable/undecodable connections.env) must degrade AA alone,
        # never abort the whole fetch_all() sweep.
        return FetchResult(
            source="aa-coding-index",
            url=ARTIFICIAL_ANALYSIS_URL,
            fetched_at=fetched_at,
            ok=False,
            error=f"AA_API_KEY resolver failed: {exc}",
        )
    if not api_key:
        return FetchResult(
            source="aa-coding-index",
            url=ARTIFICIAL_ANALYSIS_URL,
            fetched_at=fetched_at,
            ok=False,
            error="AA_API_KEY not set",
        )
    try:
        entries: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = requests.get(
                ARTIFICIAL_ANALYSIS_URL,
                headers={"x-api-key": api_key},
                params={"page": page},
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            _cache_raw(
                "artificial-analysis" if page == 1 else f"artificial-analysis-p{page}",
                resp.text,
                "json",
            )
            payload = resp.json()
            entries.extend(payload.get("data") or [])
            pagination = payload.get("pagination") or {}
            if not pagination.get("has_more") or page >= AA_MAX_PAGES:
                break
            page += 1
        rows: list[BenchmarkRow] = []
        facts: list[ModelFacts] = []
        for entry in entries:
            name = str(entry.get("name") or entry.get("slug") or "")
            if not name:
                continue
            slug = entry.get("slug")
            model_url = (
                f"https://artificialanalysis.ai/models/{slug}"
                if slug
                else "https://artificialanalysis.ai/models"
            )
            evaluations = entry.get("evaluations") or {}
            coding_index = evaluations.get("artificial_analysis_coding_index")
            # Only ever emit aa-coding-index when AA actually published a coding
            # index for this model — silently substituting the (unrelated)
            # intelligence index would corrupt coding evidence and mislabel its
            # basis. Pricing/performance facts are still recorded either way.
            if coding_index is not None:
                rows.append(
                    BenchmarkRow(
                        benchmark="aa-coding-index",
                        model_name=name,
                        score=float(coding_index),
                        metric="percent",
                        source_url=model_url,
                        note="artificial-analysis coding index",
                    )
                )
            pricing = entry.get("pricing") or {}
            # Throughput/TTFT are published nested under entry["performance"],
            # not at the top level of the entry.
            performance = entry.get("performance") or {}
            facts.append(
                ModelFacts(
                    model_name=name,
                    prompt_usd_per_m=_coerce_float(pricing.get("price_1m_input_tokens")),
                    completion_usd_per_m=_coerce_float(pricing.get("price_1m_output_tokens")),
                    tokens_per_second=_coerce_float(
                        performance.get("median_output_tokens_per_second")
                    ),
                    ttft_seconds=_coerce_float(
                        performance.get("median_time_to_first_token_seconds")
                    ),
                )
            )
        return FetchResult(
            source="aa-coding-index",
            url=ARTIFICIAL_ANALYSIS_URL,
            fetched_at=fetched_at,
            ok=True,
            rows=rows,
            facts=facts,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort by design
        return FetchResult(
            source="aa-coding-index",
            url=ARTIFICIAL_ANALYSIS_URL,
            fetched_at=fetched_at,
            ok=False,
            error=str(exc),
        )


def _swebench_live_resolved_count(resolved: Any) -> float | None:
    if isinstance(resolved, list):
        return float(len(resolved))
    if isinstance(resolved, int | float):
        return float(resolved)
    return None


def fetch_swebench_live() -> FetchResult:
    """SWE-bench-Live leaderboard (JSONL, one submission per line) filtered to
    the `lite` split — the closest analog to SWE-bench Verified's headline
    number, and a deterministic cross-check/override on the Grok-research-
    sourced swe-bench-verified figure. Each line's `name` field bundles a
    scaffold + model ("AMI Agent + Claude-4.6-Opus"); the model is the text
    after the LAST " + " (scaffold names are not expected to contain " + ")."""
    fetched_at = utc_now_iso()
    try:
        resp = requests.get(SWEBENCH_LIVE_URL, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        _cache_raw("swebench-live", resp.text, "jsonl")
        rows: list[BenchmarkRow] = []
        for raw_line in resp.text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("set") != SWEBENCH_LIVE_SET:
                continue
            name_field = str(entry.get("name") or "")
            if " + " not in name_field:
                continue
            model_name = name_field.rsplit(" + ", 1)[-1].strip()
            total = entry.get("total")
            resolved_count = _swebench_live_resolved_count(entry.get("resolved"))
            if not model_name or not total or resolved_count is None:
                continue
            score = round(resolved_count / float(total) * 100, 2)
            rows.append(
                BenchmarkRow(
                    benchmark="swebench-live",
                    model_name=model_name,
                    score=score,
                    metric="percent",
                    source_url=str(entry.get("url") or "https://swe-bench-live.github.io/"),
                    note=(
                        f"set=lite date={entry.get('date')} "
                        f"resolved={int(resolved_count)}/{int(total)}"
                    ),
                )
            )
        return FetchResult(
            source="swebench-live",
            url=SWEBENCH_LIVE_URL,
            fetched_at=fetched_at,
            ok=True,
            rows=rows,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort by design
        return FetchResult(
            source="swebench-live",
            url=SWEBENCH_LIVE_URL,
            fetched_at=fetched_at,
            ok=False,
            error=str(exc),
        )


def fetch_all(cfg: ModelIntelConfig) -> dict[str, FetchResult]:
    return {
        "aider-polyglot": fetch_aider(),
        "openrouter-pricing": fetch_openrouter(cfg),
        "aa-coding-index": fetch_artificial_analysis(cfg),
        "swebench-live": fetch_swebench_live(),
    }
