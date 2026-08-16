"""Offline tests for the model-intel merge engine (no network, no real HOME state)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.modelintel import registry as registry_mod
from omniagentos.modelintel.config import (
    BenchmarkSpec,
    BlendComponent,
    DomainSpec,
    ModelIntelConfig,
    ModelSpec,
    build_alias_index,
    normalize_model_name,
)
from omniagentos.modelintel.research import ResearchResult
from omniagentos.modelintel.sources import BenchmarkRow, FetchResult, ModelFacts


@pytest.fixture(autouse=True)
def _no_real_rankings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(registry_mod, "FUSION_RANKINGS", tmp_path / "absent.json")


def _cfg() -> ModelIntelConfig:
    return ModelIntelConfig(
        domains=[
            DomainSpec(key="coding", title="Coding", description="d"),
        ],
        domain_blend={"coding": [BlendComponent(benchmark="bench-a", weight=1.0)]},
        benchmarks=[
            BenchmarkSpec(
                key="bench-a",
                title="Bench A",
                metric="percent",
                url="https://a",
                fetch="research",
                description="d",
            ),
            BenchmarkSpec(
                key="bench-elo",
                title="Bench Elo",
                metric="elo",
                url="https://e",
                fetch="research",
                description="d",
            ),
        ],
        models=[
            ModelSpec(
                key="alpha",
                title="Alpha",
                provider="p",
                lineage="l",
                fusion_agents=["alpha-coder"],
                aliases=["vendor/alpha"],
                priors={"coding": 0.6},
            ),
            ModelSpec(
                key="beta",
                title="Beta",
                provider="p",
                lineage="l",
                priors={"coding": 0.9},
            ),
        ],
    )


def _fetch(rows: list[BenchmarkRow], facts: list[ModelFacts] | None = None) -> FetchResult:
    return FetchResult(
        source="bench-a",
        url="https://a",
        fetched_at="2026-07-12T10:00:00Z",
        ok=True,
        rows=rows,
        facts=facts or [],
    )


def test_normalize_strips_parens_and_config_suffixes() -> None:
    assert normalize_model_name("GPT-5 (high)") == "gpt-5"
    assert normalize_model_name("claude-opus-4-8-thinking") == "claude-opus-4-8"
    assert normalize_model_name("claude-sonnet-5-high") == "claude-sonnet-5"
    assert normalize_model_name("grok-4.5-fast") == "grok-4.5-fast"  # real variant, kept
    assert normalize_model_name("Alpha Two") == "alpha-two"


def test_alias_index_resolves_key_and_aliases() -> None:
    index = build_alias_index(_cfg())
    assert index["alpha"] == "alpha"
    assert index["vendor/alpha"] == "alpha"


def test_blend_and_ranking() -> None:
    cfg = _cfg()
    rows = [
        BenchmarkRow(
            benchmark="bench-a", model_name="vendor/alpha", score=90.0, source_url="https://a"
        )
    ]
    registry = registry_mod.build(cfg, {"bench-a": _fetch(rows)}, None, None)
    alpha = registry.model_entry("alpha")
    assert alpha is not None
    # blended = (1 - 1.0/2)*0.6 + (1.0/2)*0.9 = 0.75
    assert alpha.capabilities["coding"].score == pytest.approx(0.75)
    assert "bench-a@90.0" in alpha.capabilities["coding"].basis
    # beta stays on its prior 0.9 and outranks blended alpha
    assert registry.top_by_domain["coding"] == ["beta", "alpha"]


def test_same_day_keeps_best_score_and_fresher_wins() -> None:
    cfg = _cfg()
    day1 = [BenchmarkRow(benchmark="bench-a", model_name="alpha", score=80.0, source_url="")]
    first = registry_mod.build(cfg, {"bench-a": _fetch(day1)}, None, None)
    # same-day duplicate rows keep the best score
    dupes = [
        BenchmarkRow(benchmark="bench-a", model_name="alpha", score=70.0, source_url=""),
        BenchmarkRow(benchmark="bench-a", model_name="alpha (high)", score=85.0, source_url=""),
    ]
    merged = registry_mod.build(cfg, {"bench-a": _fetch(dupes)}, None, first)
    entry = merged.model_entry("alpha")
    assert entry is not None and entry.benchmarks["bench-a"].score == 85.0
    # a failed source today keeps yesterday's rows (last-known-good)
    failed = FetchResult(
        source="bench-a", url="https://a", fetched_at="2026-07-13T10:00:00Z", ok=False, error="boom"
    )
    kept = registry_mod.build(cfg, {"bench-a": failed}, None, merged)
    entry = kept.model_entry("alpha")
    assert entry is not None and entry.benchmarks["bench-a"].score == 85.0


def test_untracked_models_go_to_watchlist_with_per_benchmark_cap() -> None:
    cfg = _cfg()
    rows = [
        BenchmarkRow(
            benchmark="bench-elo",
            model_name=f"stranger-{i}",
            score=1500.0 + i,
            metric="elo",
            source_url="",
        )
        for i in range(12)
    ]
    rows += [BenchmarkRow(benchmark="bench-a", model_name="other", score=50.0, source_url="")]
    research = ResearchResult(fetched_at="2026-07-12T10:00:00Z", ok=True, rows=rows)
    registry = registry_mod.build(cfg, {}, research, None)
    elo_rows = [w for w in registry.watchlist if w.benchmark == "bench-elo"]
    assert len(elo_rows) == registry_mod.WATCHLIST_PER_BENCHMARK
    assert elo_rows[0].score == 1511.0  # best kept first
    assert any(w.benchmark == "bench-a" for w in registry.watchlist)  # elo didn't crowd it out


def test_watchlist_carries_forward_when_research_fails() -> None:
    cfg = _cfg()
    rows = [
        BenchmarkRow(
            benchmark="bench-elo", model_name="stranger", score=1500.0, metric="elo", source_url=""
        )
    ]
    research = ResearchResult(fetched_at="2026-07-12T10:00:00Z", ok=True, rows=rows)
    first = registry_mod.build(cfg, {}, research, None)
    assert len(first.watchlist) == 1
    # next day: research fails — yesterday's watchlist survives
    failed = ResearchResult(fetched_at="2026-07-13T10:00:00Z", ok=False, error="boom")
    second = registry_mod.build(cfg, {}, failed, first)
    assert [w.model_name for w in second.watchlist] == ["stranger"]


def test_watchlist_carries_forward_per_source_not_globally() -> None:
    """A deterministic fetcher's (e.g. swebench-live) untracked watchlist rows
    must survive when THAT source fails today, even if research succeeds —
    the old all-or-nothing "seed only when research failed" gate dropped them."""
    cfg = _cfg()
    research_rows = [
        BenchmarkRow(
            benchmark="bench-elo",
            model_name="research-stranger",
            score=1500.0,
            metric="elo",
            source_url="",
        )
    ]
    swebench_rows = [
        BenchmarkRow(
            benchmark="swebench-live", model_name="swe-stranger", score=42.0, source_url=""
        )
    ]
    research = ResearchResult(fetched_at="2026-07-12T10:00:00Z", ok=True, rows=research_rows)
    swebench_fetch = FetchResult(
        source="swebench-live",
        url="https://s",
        fetched_at="2026-07-12T10:00:00Z",
        ok=True,
        rows=swebench_rows,
    )
    day1 = registry_mod.build(cfg, {"swebench-live": swebench_fetch}, research, None)
    assert {w.model_name for w in day1.watchlist} == {"research-stranger", "swe-stranger"}

    # day 2: research succeeds again (nothing new this time), swebench-live FAILS
    research_day2 = ResearchResult(fetched_at="2026-07-13T10:00:00Z", ok=True, rows=[])
    swebench_failed = FetchResult(
        source="swebench-live",
        url="https://s",
        fetched_at="2026-07-13T10:00:00Z",
        ok=False,
        error="down",
    )
    day2 = registry_mod.build(cfg, {"swebench-live": swebench_failed}, research_day2, day1)
    names = {w.model_name for w in day2.watchlist}
    # swebench-live failed today -> its row carries forward (per-source last-known-good)
    assert "swe-stranger" in names
    # research succeeded today with nothing new -> its stale row is NOT blindly kept
    assert "research-stranger" not in names


def test_elo_never_blends_into_scores() -> None:
    cfg = _cfg()
    rows = [
        BenchmarkRow(
            benchmark="bench-elo", model_name="alpha", score=1600.0, metric="elo", source_url=""
        )
    ]
    research = ResearchResult(fetched_at="2026-07-12T10:00:00Z", ok=True, rows=rows)
    registry = registry_mod.build(cfg, {}, research, None)
    alpha = registry.model_entry("alpha")
    assert alpha is not None
    assert alpha.benchmarks["bench-elo"].score == 1600.0  # recorded as evidence
    assert alpha.capabilities["coding"].basis == ["prior"]  # but never blended


def test_openrouter_pricing_is_recorded_as_facts_but_never_scored() -> None:
    cfg = _cfg()
    fetch = FetchResult(
        source="openrouter-pricing",
        url="https://o",
        fetched_at="2026-07-12T10:00:00Z",
        ok=True,
        facts=[
            ModelFacts(
                model_name="vendor/alpha",
                prompt_usd_per_m=30.0,
                completion_usd_per_m=60.0,
                context_length=200_000,
            )
        ],
    )
    registry = registry_mod.build(cfg, {"openrouter-pricing": fetch}, None, None)
    alpha = registry.model_entry("alpha")
    assert alpha is not None and alpha.pricing is not None
    assert alpha.pricing.context_length == 200_000
    assert alpha.pricing.prompt_usd_per_m == 30.0
    # Price is telemetry only: routing optimizes for quality and speed, so an
    # expensive model must NOT acquire a cost capability of any kind.
    assert "cost-efficiency" not in alpha.capabilities
    assert not any("/M" in b for c in alpha.capabilities.values() for b in c.basis)


def test_digest_written_for_launchable_agents_only(tmp_path: Path) -> None:
    cfg = _cfg()
    registry = registry_mod.build(cfg, {}, None, None)
    target = tmp_path / "digest.json"
    registry_mod.write_fusion_digest(cfg, registry, target)
    digest = __import__("json").loads(target.read_text())
    assert [a["id"] for a in digest["agents"]] == ["alpha-coder"]  # beta has no agents
    assert digest["topByDomain"]["coding"]


# --- Artificial Analysis merge (per-source fallback, never openrouter-only) --


def _aa_fetch(facts: list[ModelFacts], rows: list[BenchmarkRow] | None = None) -> FetchResult:
    return FetchResult(
        source="aa-coding-index",
        url="https://aa",
        fetched_at="2026-07-12T10:00:00Z",
        ok=True,
        rows=rows or [],
        facts=facts,
    )


def test_aa_pricing_fills_gap_openrouter_never_saw() -> None:
    cfg = _cfg()
    fetch = _aa_fetch([ModelFacts(model_name="vendor/alpha", prompt_usd_per_m=4.0)])
    registry = registry_mod.build(cfg, {"aa-coding-index": fetch}, None, None)
    alpha = registry.model_entry("alpha")
    assert alpha is not None and alpha.pricing is not None
    assert alpha.pricing.prompt_usd_per_m == 4.0
    assert alpha.pricing.source == "artificial-analysis"


def test_openrouter_pricing_wins_over_aa_when_both_present() -> None:
    cfg = _cfg()
    openrouter = FetchResult(
        source="openrouter-pricing",
        url="https://o",
        fetched_at="2026-07-12T10:00:00Z",
        ok=True,
        facts=[ModelFacts(model_name="vendor/alpha", prompt_usd_per_m=1.0)],
    )
    aa = _aa_fetch([ModelFacts(model_name="vendor/alpha", prompt_usd_per_m=9.0)])
    registry = registry_mod.build(
        cfg, {"openrouter-pricing": openrouter, "aa-coding-index": aa}, None, None
    )
    alpha = registry.model_entry("alpha")
    assert alpha is not None and alpha.pricing is not None
    assert alpha.pricing.prompt_usd_per_m == 1.0
    assert alpha.pricing.source == "openrouter"


def test_openrouter_missing_prompt_price_falls_back_to_aa_but_keeps_context() -> None:
    """A partial OpenRouter record (no prompt price yet) must not block AA from
    filling JUST that field — and must not lose OpenRouter's own completion
    price/context in the process."""
    cfg = _cfg()
    openrouter = FetchResult(
        source="openrouter-pricing",
        url="https://o",
        fetched_at="2026-07-12T10:00:00Z",
        ok=True,
        facts=[
            ModelFacts(
                model_name="vendor/alpha",
                prompt_usd_per_m=None,  # OpenRouter hasn't priced this model yet
                completion_usd_per_m=12.0,
                context_length=128_000,
            )
        ],
    )
    aa = _aa_fetch(
        [ModelFacts(model_name="vendor/alpha", prompt_usd_per_m=4.0, completion_usd_per_m=20.0)]
    )
    registry = registry_mod.build(
        cfg, {"openrouter-pricing": openrouter, "aa-coding-index": aa}, None, None
    )
    alpha = registry.model_entry("alpha")
    assert alpha is not None and alpha.pricing is not None
    assert alpha.pricing.prompt_usd_per_m == 4.0  # filled from AA
    assert alpha.pricing.completion_usd_per_m == 12.0  # OpenRouter's own value preserved
    assert alpha.pricing.context_length == 128_000  # OpenRouter context preserved


def test_openrouter_zero_price_is_not_treated_as_missing() -> None:
    """A real $0/M price is data, not "missing" — truthiness checks must never
    let AA/previous override it in the recorded pricing facts."""
    cfg = _cfg()
    openrouter = FetchResult(
        source="openrouter-pricing",
        url="https://o",
        fetched_at="2026-07-12T10:00:00Z",
        ok=True,
        facts=[ModelFacts(model_name="vendor/alpha", prompt_usd_per_m=0.0)],
    )
    aa = _aa_fetch([ModelFacts(model_name="vendor/alpha", prompt_usd_per_m=9.0)])
    registry = registry_mod.build(
        cfg, {"openrouter-pricing": openrouter, "aa-coding-index": aa}, None, None
    )
    alpha = registry.model_entry("alpha")
    assert alpha is not None and alpha.pricing is not None
    assert alpha.pricing.prompt_usd_per_m == 0.0  # a real zero, not "missing"
    assert alpha.pricing.source == "openrouter"  # AA's $9 never overrides a real $0


def test_previous_pricing_survives_when_no_live_source_has_it() -> None:
    cfg = _cfg()
    prev = registry_mod.build(
        cfg,
        {
            "openrouter-pricing": FetchResult(
                source="openrouter-pricing",
                url="https://o",
                fetched_at="2026-07-11T10:00:00Z",
                ok=True,
                facts=[ModelFacts(model_name="vendor/alpha", prompt_usd_per_m=2.0)],
            )
        },
        None,
        None,
    )
    today = registry_mod.build(cfg, {}, None, prev)  # neither live source ran today
    alpha = today.model_entry("alpha")
    assert alpha is not None and alpha.pricing is not None
    assert alpha.pricing.prompt_usd_per_m == 2.0
    assert alpha.pricing.source == "openrouter"


def test_openrouter_latency_cross_check_flows_into_pricing() -> None:
    cfg = _cfg()
    fetch = FetchResult(
        source="openrouter-pricing",
        url="https://o",
        fetched_at="2026-07-12T10:00:00Z",
        ok=True,
        facts=[ModelFacts(model_name="vendor/alpha", prompt_usd_per_m=3.0, latency_ms_p50=850.0)],
    )
    registry = registry_mod.build(cfg, {"openrouter-pricing": fetch}, None, None)
    alpha = registry.model_entry("alpha")
    assert alpha is not None and alpha.pricing is not None
    assert alpha.pricing.latency_ms_p50 == 850.0


def test_aa_speed_fallback_only_applies_without_measured_latency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = ModelIntelConfig(
        domains=[DomainSpec(key="speed", title="Speed", description="d")],
        models=[
            ModelSpec(
                key="measured",
                title="Measured",
                provider="p",
                lineage="l",
                fusion_agents=["measured-coder"],
                aliases=["vendor/measured"],
                priors={"speed": 0.5},
            ),
            ModelSpec(
                key="unmeasured",
                title="Unmeasured",
                provider="p",
                lineage="l",
                aliases=["vendor/unmeasured"],
                priors={"speed": 0.5},
            ),
        ],
    )
    rankings = tmp_path / "rankings.json"
    rankings.write_text(
        __import__("json").dumps({"agents": [{"id": "measured-coder", "warmLatencyMs": 1000}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(registry_mod, "FUSION_RANKINGS", rankings)
    aa = _aa_fetch(
        [
            ModelFacts(model_name="vendor/measured", tokens_per_second=300.0),
            ModelFacts(model_name="vendor/unmeasured", tokens_per_second=20.0),
        ]
    )
    registry = registry_mod.build(cfg, {"aa-coding-index": aa}, None, None)

    measured = registry.model_entry("measured")
    unmeasured = registry.model_entry("unmeasured")
    assert measured is not None and unmeasured is not None
    # measured-latency wins for "measured" — AA tok/s is present but ignored
    assert any("measured-latency" in b for b in measured.capabilities["speed"].basis)
    assert not any("aa-tokens-per-sec" in b for b in measured.capabilities["speed"].basis)
    # "unmeasured" has no fusion agent -> falls back to the AA tok/s proxy
    assert any("aa-tokens-per-sec@20.0" in b for b in unmeasured.capabilities["speed"].basis)
    # 20 tok/s is slow -> pulls the blended score below the 0.5 prior
    assert unmeasured.capabilities["speed"].score < 0.5


def _speed_only_cfg(fusion_agents: list[str] | None = None) -> ModelIntelConfig:
    return ModelIntelConfig(
        domains=[DomainSpec(key="speed", title="Speed", description="d")],
        models=[
            ModelSpec(
                key="reference",
                title="Reference",
                provider="p",
                lineage="l",
                fusion_agents=fusion_agents or [],
                aliases=["vendor/reference"],
                priors={"speed": 0.5},
            ),
        ],
    )


def test_aa_speed_persists_across_a_failed_fetch() -> None:
    """A model's AA-derived speed must NOT revert to its bare prior just
    because AA fails/disappears one day — it stays seeded from the last
    successful fetch (per-source fallback, same rule as everything else)."""
    cfg = _speed_only_cfg()
    day1_aa = _aa_fetch([ModelFacts(model_name="vendor/reference", tokens_per_second=300.0)])
    day1 = registry_mod.build(cfg, {"aa-coding-index": day1_aa}, None, None)
    ref1 = day1.model_entry("reference")
    assert ref1 is not None
    # (0.5 prior + 0.95 clipped aa-speed) / 2 = 0.725 — matches the finding's example
    assert ref1.capabilities["speed"].score == pytest.approx(0.725, abs=0.005)
    assert ref1.aa_speed is not None and ref1.aa_speed.tokens_per_second == 300.0

    day2_failed = FetchResult(
        source="aa-coding-index",
        url="https://aa",
        fetched_at="2026-07-13T10:00:00Z",
        ok=False,
        error="AA down",
    )
    day2 = registry_mod.build(cfg, {"aa-coding-index": day2_failed}, None, day1)
    ref2 = day2.model_entry("reference")
    assert ref2 is not None
    # AA failed today, but yesterday's throughput carries forward
    assert ref2.capabilities["speed"].score == pytest.approx(0.725, abs=0.005)
    assert any("aa-tokens-per-sec@300.0" in b for b in ref2.capabilities["speed"].basis)
    assert ref2.aa_speed is not None and ref2.aa_speed.tokens_per_second == 300.0


def test_aa_speed_fallback_never_applies_to_launchable_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The AA speed fallback is scoped to models with NO fusion_agents — a
    launchable model with a missing/corrupt CURRENT latency reading must not
    silently pick up the AA proxy instead."""
    cfg = _speed_only_cfg(fusion_agents=["alpha-coder"])
    # rankings file exists but has no entry for alpha-coder -> no measured latency today
    rankings = tmp_path / "rankings.json"
    rankings.write_text(__import__("json").dumps({"agents": []}), encoding="utf-8")
    monkeypatch.setattr(registry_mod, "FUSION_RANKINGS", rankings)

    aa = _aa_fetch([ModelFacts(model_name="vendor/reference", tokens_per_second=300.0)])
    registry = registry_mod.build(cfg, {"aa-coding-index": aa}, None, None)
    reference = registry.model_entry("reference")
    assert reference is not None
    assert reference.capabilities["speed"].basis == ["prior"]
    assert reference.capabilities["speed"].score == pytest.approx(0.5)


# --- write_fusion_rankings ----------------------------------------------


def _rankings_cfg() -> ModelIntelConfig:
    return ModelIntelConfig(
        domains=[
            DomainSpec(key="coding-implementation", title="Coding", description="d"),
            DomainSpec(key="agentic-tool-use", title="Tool use", description="d"),
        ],
        models=[
            ModelSpec(
                key="alpha",
                title="Alpha",
                provider="p",
                lineage="l",
                fusion_agents=["alpha-coder"],
                aliases=["vendor/alpha"],
                priors={
                    "coding-implementation": 0.8,
                    "agentic-tool-use": 0.7,
                },
            )
        ],
    )


def test_write_fusion_rankings_no_op_when_file_missing(tmp_path: Path) -> None:
    cfg = _rankings_cfg()
    registry = registry_mod.build(cfg, {}, None, None)
    target = tmp_path / "missing.json"
    assert registry_mod.write_fusion_rankings(cfg, registry, target) is None
    assert not target.exists()


def test_write_fusion_rankings_updates_matched_agents_only(tmp_path: Path) -> None:
    cfg = _rankings_cfg()
    registry = registry_mod.build(cfg, {}, None, None)
    target = tmp_path / "rankings.json"
    original = {
        "schema": 1,
        "host": {"cores": 8},
        "agents": [
            {
                "id": "alpha-coder",
                "available": True,
                "warmLatencyMs": 1234,
                "codingScore": 0.1,
                "toolUseScore": 0.1,
                "costScore": 0.1,
            },
            {"id": "unrelated-agent", "codingScore": 0.9},
        ],
    }
    target.write_text(__import__("json").dumps(original), encoding="utf-8")

    result = registry_mod.write_fusion_rankings(cfg, registry, target)
    assert result == target
    written = __import__("json").loads(target.read_text())

    alpha_agent = next(a for a in written["agents"] if a["id"] == "alpha-coder")
    assert (
        alpha_agent["codingScore"]
        == registry.model_entry("alpha").capabilities["coding-implementation"].score
    )
    assert (
        alpha_agent["toolUseScore"]
        == registry.model_entry("alpha").capabilities["agentic-tool-use"].score
    )
    # a stale costScore from an older build is evicted, not refreshed
    assert "costScore" not in alpha_agent
    # untouched fields survive verbatim
    assert alpha_agent["available"] is True
    assert alpha_agent["warmLatencyMs"] == 1234
    assert written["host"] == {"cores": 8}
    # unrelated agent (no matching tracked model) is left completely alone
    unrelated = next(a for a in written["agents"] if a["id"] == "unrelated-agent")
    assert unrelated == {"id": "unrelated-agent", "codingScore": 0.9}
    assert written["scoresUpdatedAt"] == registry.updated_at


def test_write_fusion_rankings_no_op_when_no_agent_matches(tmp_path: Path) -> None:
    cfg = _rankings_cfg()
    registry = registry_mod.build(cfg, {}, None, None)
    target = tmp_path / "rankings.json"
    original = {"agents": [{"id": "totally-unrelated", "codingScore": 0.5}]}
    target.write_text(__import__("json").dumps(original), encoding="utf-8")
    assert registry_mod.write_fusion_rankings(cfg, registry, target) is None
    # file is left byte-identical when nothing changed
    assert __import__("json").loads(target.read_text()) == original


@pytest.mark.parametrize("payload", [[], None, "text", 42, [{"agents": []}]])
def test_write_fusion_rankings_degrades_on_structurally_wrong_json(
    tmp_path: Path, payload: object
) -> None:
    """A syntactically-valid but structurally-wrong-shaped rankings file (e.g.
    a top-level array) must degrade to None, not crash with AttributeError."""
    cfg = _rankings_cfg()
    registry = registry_mod.build(cfg, {}, None, None)
    target = tmp_path / "rankings.json"
    original_text = __import__("json").dumps(payload)
    target.write_text(original_text, encoding="utf-8")
    assert registry_mod.write_fusion_rankings(cfg, registry, target) is None
    # file must be left byte-identical — a graceful no-op, never a partial write
    assert target.read_text(encoding="utf-8") == original_text


def test_write_fusion_rankings_atomic_write_failure_leaves_original_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process death / write error mid-write (simulated here via a failing
    fsync) must leave the ORIGINAL live-router file byte-identical — never a
    truncated or partial JSON write — and must not leave a stray temp file."""
    cfg = _rankings_cfg()
    registry = registry_mod.build(cfg, {}, None, None)
    target = tmp_path / "rankings.json"
    original = {
        "agents": [{"id": "alpha-coder", "codingScore": 0.1, "toolUseScore": 0.1, "costScore": 0.1}]
    }
    original_text = __import__("json").dumps(original)
    target.write_text(original_text, encoding="utf-8")

    def _boom_fsync(fd: int) -> None:
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(registry_mod.os, "fsync", _boom_fsync)

    with pytest.raises(OSError):
        registry_mod.write_fusion_rankings(cfg, registry, target)

    # original file must be byte-identical — no truncation, no partial JSON
    assert target.read_text(encoding="utf-8") == original_text
    # no leftover temp file in the directory
    leftovers = [p for p in tmp_path.iterdir() if p != target]
    assert leftovers == []


def test_atomic_write_text_cleans_up_temp_file_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out.json"
    target.write_text('{"original": true}', encoding="utf-8")

    def _boom_replace(src: str, dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(registry_mod.os, "replace", _boom_replace)

    with pytest.raises(OSError):
        registry_mod._atomic_write_text(target, '{"new": true}')

    assert target.read_text(encoding="utf-8") == '{"original": true}'
    leftovers = [p for p in tmp_path.iterdir() if p != target]
    assert leftovers == []
