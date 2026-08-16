"""Registry builder — merges curated priors with live benchmark evidence into
the canonical machine-readable capability registry.

Merge rules (deliberately simple and inspectable):
- Fresher `as_of` always wins; same-day duplicates for one (model, benchmark)
  keep the best score (leaderboards list several configs per model; the
  headline is what the best config does).
- A source that failed today keeps yesterday's rows (last-known-good) — the
  registry degrades stale, never empty.
- Percent benchmarks blend into domain scores per configs/modelintel.yaml
  `domain_blend`: blended = (1 - W/2)*prior + Σ(w_i/2 * score_i/100) where
  W = Σw_i over benchmarks with data. Elo boards rank in vault notes but
  never blend (cross-board Elo normalization is a lie).
- speed comes from locally measured CLI latency (~/.claude/fusion/
  model-rankings.json), blended 50/50 with the prior.
- price is NEVER scored and never influences routing. The OpenRouter fetch is
  retained only because it is the sole live source of per-model latency and
  context length; its pricing fields are carried as facts for display.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from omniagentos.contracts import utc_now_iso
from omniagentos.modelintel.config import (
    FUSION_DIGEST,
    FUSION_RANKINGS,
    ModelIntelConfig,
    build_alias_index,
    normalize_model_name,
    registry_path,
)
from omniagentos.modelintel.research import ReleaseNote, ResearchResult
from omniagentos.modelintel.sources import BenchmarkRow, FetchResult, ModelFacts

WATCHLIST_PER_BENCHMARK = 8


class BenchmarkEntry(BaseModel):
    score: float
    metric: str
    source: str  # fetcher key or "grok-research"
    source_url: str = ""
    as_of: str


class Pricing(BaseModel):
    prompt_usd_per_m: float | None = None
    completion_usd_per_m: float | None = None
    context_length: int | None = None
    latency_ms_p50: float | None = None  # OpenRouter endpoint-latency cross-check
    as_of: str
    source: str = "openrouter"


class AaSpeedFact(BaseModel):
    """Artificial Analysis live throughput, persisted across days so a single
    failed AA fetch never reverts a reference model's speed score to its prior
    (per-source fallback — see `aa_speed_by_model` in build())."""

    tokens_per_second: float
    as_of: str


class CapabilityScore(BaseModel):
    score: float
    basis: list[str] = Field(default_factory=list)


class ModelEntry(BaseModel):
    key: str
    title: str
    provider: str
    lineage: str
    fusion_agents: list[str] = Field(default_factory=list)
    pricing: Pricing | None = None
    measured_latency_ms: int | None = None
    aa_speed: AaSpeedFact | None = None
    benchmarks: dict[str, BenchmarkEntry] = Field(default_factory=dict)
    capabilities: dict[str, CapabilityScore] = Field(default_factory=dict)


class SourceStatus(BaseModel):
    ok: bool
    fetched_at: str
    error: str | None = None


class Registry(BaseModel):
    schema_version: int = 1
    updated_at: str
    sources: dict[str, SourceStatus] = Field(default_factory=dict)
    models: list[ModelEntry] = Field(default_factory=list)
    watchlist: list[BenchmarkRow] = Field(default_factory=list)
    releases: list[ReleaseNote] = Field(default_factory=list)
    top_by_domain: dict[str, list[str]] = Field(default_factory=dict)

    def model_entry(self, key: str) -> ModelEntry | None:
        return next((m for m in self.models if m.key == key), None)


def load_previous(path: Path | None = None) -> Registry | None:
    target = path or registry_path()
    if not target.is_file():
        return None
    try:
        return Registry(**json.loads(target.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 - a corrupt registry must not block a rebuild
        return None


def _fusion_latency_by_model(cfg: ModelIntelConfig) -> dict[str, int]:
    """Model key -> fastest measured warm latency among its Fusion agents."""
    if not FUSION_RANKINGS.is_file():
        return {}
    try:
        rankings = json.loads(FUSION_RANKINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    latency_by_agent = {
        a["id"]: a.get("warmLatencyMs")
        for a in rankings.get("agents", [])
        if a.get("warmLatencyMs")
    }
    result: dict[str, int] = {}
    for model in cfg.models:
        measured = [latency_by_agent[a] for a in model.fusion_agents if a in latency_by_agent]
        if measured:
            result[model.key] = int(min(measured))
    return result


def _aa_speed_score(tokens_per_second: float) -> float:
    """Artificial Analysis live tokens/sec -> a speed proxy, used ONLY as a
    fallback for models with no locally-measured Fusion CLI latency (i.e. no
    fusion_agents — most of the reference-only roster). 20 tok/s -> ~0.16,
    60 -> ~0.49, 150 -> ~0.77, 300 -> ~0.95 (clipped)."""
    score = 0.7 * math.log10(max(tokens_per_second, 1.0)) - 0.75
    return max(0.05, min(0.95, score))


def _aa_facts_by_model(
    fetch: FetchResult | None, alias_index: dict[str, str]
) -> dict[str, ModelFacts]:
    """model key -> Artificial Analysis facts, when the fetch succeeded."""
    result: dict[str, ModelFacts] = {}
    if not fetch or not fetch.ok:
        return result
    for fact in fetch.facts:
        model_key = alias_index.get(normalize_model_name(fact.model_name))
        if model_key:
            result[model_key] = fact
    return result


def build(
    cfg: ModelIntelConfig,
    fetches: dict[str, FetchResult],
    research: ResearchResult | None,
    previous: Registry | None,
) -> Registry:
    now = utc_now_iso()
    alias_index = build_alias_index(cfg)
    latency_by_model = _fusion_latency_by_model(cfg)
    aa_facts_by_model = _aa_facts_by_model(fetches.get("aa-coding-index"), alias_index)

    # AA throughput is persisted across days (seeded from the previous registry,
    # replaced only by valid fresh facts) so a failed/absent AA fetch never
    # reverts a reference model's speed score to its bare prior — same
    # per-source fallback rule as everything else here.
    aa_speed_by_model: dict[str, AaSpeedFact] = {}
    if previous:
        for prev_model in previous.models:
            if prev_model.aa_speed:
                aa_speed_by_model[prev_model.key] = prev_model.aa_speed
    aa_fetch_for_speed = fetches.get("aa-coding-index")
    if aa_fetch_for_speed and aa_fetch_for_speed.ok:
        for known_model_key, fact in aa_facts_by_model.items():
            if fact.tokens_per_second is not None:
                aa_speed_by_model[known_model_key] = AaSpeedFact(
                    tokens_per_second=fact.tokens_per_second, as_of=aa_fetch_for_speed.fetched_at
                )

    # --- collect benchmark rows: previous (stale base) -> research -> fetchers ---
    benchmarks: dict[str, dict[str, BenchmarkEntry]] = {m.key: {} for m in cfg.models}
    if previous:
        for prev_model in previous.models:
            if prev_model.key in benchmarks:
                benchmarks[prev_model.key].update(prev_model.benchmarks)

    watchlist: dict[tuple[str, str], BenchmarkRow] = {}
    if previous:
        # Carry forward previous watchlist rows PER SOURCE, not globally: a
        # deterministic fetcher (e.g. swebench-live) that fails today must not
        # lose its untracked rows just because research succeeded today (and
        # vice versa) — same last-known-good rule as tracked benchmarks, but
        # keyed off each row's own origin source, not the sweep as a whole.
        source_ok_today: dict[str, bool] = {key: f.ok for key, f in fetches.items()}
        if research is not None:
            source_ok_today["grok-research"] = research.ok
        for row in previous.watchlist:
            if source_ok_today.get(row.source, False):
                continue  # that source ran fresh today — it re-derives its own rows
            watchlist[(row.benchmark, normalize_model_name(row.model_name))] = row

    def absorb(rows: list[BenchmarkRow], source: str, as_of: str) -> None:
        for row in rows:
            if row.score is None:
                continue
            model_key = alias_index.get(normalize_model_name(row.model_name))
            if model_key is None:
                key = (row.benchmark, normalize_model_name(row.model_name))
                kept = watchlist.get(key)
                if kept is None or (kept.score or 0) < row.score:
                    # stamp provenance so a future day can carry this row forward
                    # source-by-source if THIS source fails (see seeding above).
                    watchlist[key] = row.model_copy(update={"source": source, "as_of": as_of})
                continue
            existing = benchmarks[model_key].get(row.benchmark)
            if existing is not None:
                if existing.as_of > as_of:
                    continue  # never replace fresher data with older
                if existing.as_of == as_of and existing.score >= row.score:
                    continue  # duplicates from the same day keep the best score
            benchmarks[model_key][row.benchmark] = BenchmarkEntry(
                score=row.score,
                metric=row.metric,
                source=source,
                source_url=row.source_url,
                as_of=as_of,
            )

    if research and research.ok:
        absorb(research.rows, "grok-research", research.fetched_at)
    for fetch in fetches.values():
        if fetch.ok and fetch.rows:
            absorb(fetch.rows, fetch.source, fetch.fetched_at)

    # --- pricing facts: openrouter (primary, live) -> artificial-analysis
    # (secondary, live, fills models openrouter doesn't list) -> previous day
    # (stale cache) — per-source fallback, PER FIELD: a partial OpenRouter
    # record (e.g. missing prompt_usd_per_m) must still let AA/previous fill
    # just the missing field(s), never block the whole fallback chain. Uses
    # `is not None` throughout — a real $0/M price is data, not "missing".
    pricing_by_model: dict[str, Pricing] = {}

    def _apply_pricing(
        model_key: str,
        prompt: float | None,
        completion: float | None,
        context: int | None,
        as_of: str,
        source: str,
    ) -> None:
        existing = pricing_by_model.get(model_key)
        if existing is None:
            pricing_by_model[model_key] = Pricing(
                prompt_usd_per_m=prompt,
                completion_usd_per_m=completion,
                context_length=context,
                as_of=as_of,
                source=source,
            )
            return
        if existing.prompt_usd_per_m is None and prompt is not None:
            existing.prompt_usd_per_m = prompt
            existing.as_of = as_of
            existing.source = source
        if existing.completion_usd_per_m is None and completion is not None:
            existing.completion_usd_per_m = completion
        if existing.context_length is None and context is not None:
            existing.context_length = context

    openrouter = fetches.get("openrouter-pricing")
    if openrouter and openrouter.ok:
        for fact in openrouter.facts:
            model_key = alias_index.get(normalize_model_name(fact.model_name))
            if model_key:
                _apply_pricing(
                    model_key,
                    fact.prompt_usd_per_m,
                    fact.completion_usd_per_m,
                    fact.context_length,
                    openrouter.fetched_at,
                    "openrouter",
                )
                if fact.latency_ms_p50 is not None:
                    # live-only cross-check: never carried forward from a stale
                    # previous-day record, only ever set by today's OpenRouter fetch.
                    pricing_by_model[model_key].latency_ms_p50 = fact.latency_ms_p50
    aa_fetch = fetches.get("aa-coding-index")
    if aa_fetch and aa_fetch.ok:
        for model_key, fact in aa_facts_by_model.items():
            _apply_pricing(
                model_key,
                fact.prompt_usd_per_m,
                fact.completion_usd_per_m,
                None,
                aa_fetch.fetched_at,
                "artificial-analysis",
            )
    if previous:
        for prev_model in previous.models:
            if prev_model.pricing:
                _apply_pricing(
                    prev_model.key,
                    prev_model.pricing.prompt_usd_per_m,
                    prev_model.pricing.completion_usd_per_m,
                    prev_model.pricing.context_length,
                    prev_model.pricing.as_of,
                    prev_model.pricing.source,
                )

    # --- capability scores ---
    models: list[ModelEntry] = []
    for spec in cfg.models:
        model_benchmarks = benchmarks[spec.key]
        capabilities: dict[str, CapabilityScore] = {}
        for domain in cfg.domains:
            prior = spec.priors.get(domain.key, 0.5)
            score, basis = prior, ["prior"]
            components = [
                (c, model_benchmarks[c.benchmark])
                for c in cfg.domain_blend.get(domain.key, [])
                if c.benchmark in model_benchmarks
                and model_benchmarks[c.benchmark].metric == "percent"
            ]
            if components:
                total_weight = sum(c.weight for c, _ in components)
                score = (1 - total_weight / 2) * prior + sum(
                    (c.weight / 2) * (entry.score / 100) for c, entry in components
                )
                basis += [f"{c.benchmark}@{entry.score}" for c, entry in components]
            if domain.key == "speed" and spec.key in latency_by_model:
                fastest = min(latency_by_model.values())
                measured = 0.95 * fastest / latency_by_model[spec.key]
                score = (prior + measured) / 2
                basis += [f"measured-latency@{latency_by_model[spec.key]}ms"]
            elif domain.key == "speed" and not spec.fusion_agents and spec.key in aa_speed_by_model:
                # Fallback ONLY for models with no Fusion CLI (no fusion_agents,
                # so no warm-latency measurement is possible at all) — most of
                # the reference-only roster. A launchable model with fusion_agents
                # but a missing/corrupt CURRENT latency reading must NOT fall
                # back to this proxy (design intent: never overrides, and never
                # substitutes for, a real measurement path). Uses the persisted
                # fact (seeded from previous days) so one failed AA fetch never
                # reverts the score to the bare prior.
                aa_speed = _aa_speed_score(aa_speed_by_model[spec.key].tokens_per_second)
                score = (prior + aa_speed) / 2
                basis += [f"aa-tokens-per-sec@{aa_speed_by_model[spec.key].tokens_per_second}"]
            capabilities[domain.key] = CapabilityScore(
                score=round(max(0.0, min(1.0, score)), 3), basis=basis
            )
        models.append(
            ModelEntry(
                key=spec.key,
                title=spec.title,
                provider=spec.provider,
                lineage=spec.lineage,
                fusion_agents=spec.fusion_agents,
                pricing=pricing_by_model.get(spec.key),
                measured_latency_ms=latency_by_model.get(spec.key),
                aa_speed=aa_speed_by_model.get(spec.key),
                benchmarks=model_benchmarks,
                capabilities=capabilities,
            )
        )

    top_by_domain = {
        domain.key: [
            m.key
            for m in sorted(models, key=lambda m: m.capabilities[domain.key].score, reverse=True)
        ]
        for domain in cfg.domains
    }

    sources = {
        key: SourceStatus(ok=f.ok, fetched_at=f.fetched_at, error=f.error)
        for key, f in fetches.items()
    }
    if research:
        sources["grok-research"] = SourceStatus(
            ok=research.ok, fetched_at=research.fetched_at, error=research.error
        )

    # Cap the watchlist PER BENCHMARK, not globally — Elo boards (1500+) would
    # otherwise crowd every percent-metric board out of the global top-N.
    by_bench: dict[str, list[BenchmarkRow]] = {}
    for row in watchlist.values():
        by_bench.setdefault(row.benchmark, []).append(row)
    watchlist_rows: list[BenchmarkRow] = []
    for rows in by_bench.values():
        rows.sort(key=lambda r: r.score or 0, reverse=True)
        watchlist_rows.extend(rows[:WATCHLIST_PER_BENCHMARK])
    releases = (
        research.releases if research and research.ok else (previous.releases if previous else [])
    )

    return Registry(
        updated_at=now,
        sources=sources,
        models=models,
        watchlist=watchlist_rows,
        releases=releases,
        top_by_domain=top_by_domain,
    )


def _atomic_write_text(target: Path, content: str) -> None:
    """Write `content` to `target` atomically: write+flush+fsync a temp file in
    the SAME directory, then commit with os.replace() (a single filesystem
    rename, guaranteed atomic on the same volume). A process death, write
    error, or full disk at any point before the final rename leaves the
    ORIGINAL file byte-identical — never a truncated/partial write. The temp
    file is cleaned up on failure."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save(registry: Registry, path: Path | None = None) -> Path:
    target = path or registry_path()
    _atomic_write_text(target, registry.model_dump_json(indent=2) + "\n")
    return target


def write_fusion_digest(
    cfg: ModelIntelConfig, registry: Registry, path: Path | None = None
) -> Path:
    """Compact per-AGENT digest for the Fusion skills and the Grok router —
    small enough to inline into a routing prompt."""
    agents = []
    for model in registry.models:
        for agent_id in model.fusion_agents:
            agents.append(
                {
                    "id": agent_id,
                    "model": model.key,
                    "provider": model.provider,
                    "lineage": model.lineage,
                    "scores": {k: v.score for k, v in model.capabilities.items()},
                    "latencyMs": model.measured_latency_ms,
                    "promptUsdPerM": model.pricing.prompt_usd_per_m if model.pricing else None,
                }
            )
    digest = {
        "schema": 1,
        "updatedAt": registry.updated_at,
        "domains": {d.key: d.title for d in cfg.domains},
        "agents": agents,
        "topByDomain": registry.top_by_domain,
        "registryPath": str(registry_path()),
    }
    target = path or FUSION_DIGEST
    _atomic_write_text(target, json.dumps(digest, indent=2) + "\n")
    return target


class RankingsRefresh(BaseModel):
    """Outcome of a fusion-rankings refresh, with the REASON a no-op happened.

    ``absent`` (file not created yet) is a benign, expected state; ``unreadable``
    (corrupt JSON / IO error) and ``wrong-shape`` are FAILURES — Fusion keeps
    routing on stale hand-tuned constants — and must never be reported to the
    operator with the same wording as ``absent``.
    """

    path: Path | None = None
    status: str  # "written" | "absent" | "unreadable" | "wrong-shape" | "no-match"

    @property
    def ok(self) -> bool:
        return self.status == "written"

    def describe(self) -> str:
        if self.path is not None:
            return str(self.path)
        return {
            "absent": "skipped (no model-rankings.json)",
            "unreadable": "FAILED (model-rankings.json unreadable/corrupt)",
            "wrong-shape": "FAILED (model-rankings.json has an unexpected shape)",
            "no-match": "skipped (no agent in model-rankings.json maps to a tracked model)",
        }.get(self.status, f"skipped ({self.status})")


def refresh_fusion_rankings(
    cfg: ModelIntelConfig, registry: Registry, path: Path | None = None
) -> RankingsRefresh:
    """:func:`write_fusion_rankings` with the no-op REASON preserved."""
    target = path or FUSION_RANKINGS
    if not target.is_file():
        return RankingsRefresh(status="absent")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return RankingsRefresh(status="unreadable")
    if not isinstance(payload, dict):
        return RankingsRefresh(status="wrong-shape")
    agent_to_model = {
        agent_id: model.key for model in cfg.models for agent_id in model.fusion_agents
    }
    agents = payload.get("agents")
    if not isinstance(agents, list):
        return RankingsRefresh(status="wrong-shape")
    changed = False
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        agent_id = agent.get("id")
        model_key = agent_to_model.get(agent_id) if isinstance(agent_id, str) else None
        if not model_key:
            continue
        entry = registry.model_entry(model_key)
        if entry is None:
            continue
        caps = entry.capabilities
        if "coding-implementation" in caps:
            agent["codingScore"] = caps["coding-implementation"].score
            changed = True
        if "agentic-tool-use" in caps:
            agent["toolUseScore"] = caps["agentic-tool-use"].score
            changed = True
        # Cost must not influence routing. Actively evict any costScore left by
        # an older build so a stale value cannot keep steering Fusion's picks.
        if agent.pop("costScore", None) is not None:
            changed = True
    if not changed:
        return RankingsRefresh(status="no-match")
    payload["scoresUpdatedAt"] = registry.updated_at
    _atomic_write_text(target, json.dumps(payload, indent=2) + "\n")
    return RankingsRefresh(path=target, status="written")


def write_fusion_rankings(
    cfg: ModelIntelConfig, registry: Registry, path: Path | None = None
) -> Path | None:
    """Refresh codingScore/toolUseScore on ~/.claude/fusion/
    model-rankings.json in place, from today's merged registry, so the Fusion
    router scores agents on fresh benchmark/latency evidence instead of the
    hand-tuned constants baked into refresh-rankings.sh.

    Ownership split with refresh-rankings.sh (a live CLI probe this module
    never runs): availability, warmLatencyMs, host, and providers stay
    refresh-rankings.sh's job — this function only ever touches the three
    score fields, keyed off `fusion_agents` in configs/modelintel.yaml.

    No-op (returns None) if the rankings file doesn't exist yet, is corrupt or
    structurally unexpected (not a JSON object, or its "agents" isn't a list —
    e.g. a syntactically-valid-but-wrong-shape top-level array), or no agent in
    it maps to a tracked model — we only ever refresh scores we can attribute
    to a real agent id, never fabricate a roster from scratch, and NEVER let a
    malformed rankings file abort the daily update.

    Thin wrapper over :func:`refresh_fusion_rankings`, which additionally
    reports WHY a no-op happened (absent vs unreadable vs wrong-shape).
    """
    return refresh_fusion_rankings(cfg, registry, path).path
