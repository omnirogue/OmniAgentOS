"""Vault knowledge-graph renderer — one note per model, capability domain, and
benchmark source, plus the `model-intelligence` MOC hub.

Every note goes through omniagentos.vault.write_note (frozen 8-field
frontmatter, `## Notes (human)` preservation, path confinement) and carries
dense wikilinks so the Obsidian graph view shows the model↔capability↔benchmark
triangle. Notes are VIEWS: the machine-readable truth stays in
var/modelintel/registry.json (vault contract forbids duplicating JSON blobs).

Wikilink gotcha encoded here once: `[[target|alias]]` inside a Markdown table
cell must escape the pipe (`[[target\\|alias]]`) or the table breaks in
Obsidian — templates and the link() helper both honor that.
"""

from __future__ import annotations

from typing import Any

from omniagentos.contracts import NoteType, VaultFrontmatter, default_vault_dir, utc_now_iso
from omniagentos.modelintel.config import ModelIntelConfig
from omniagentos.modelintel.registry import ModelEntry, Registry
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.templating import render_template
from omniagentos.vault.write import write_note

TOP_N_BEST_AT = 3


def _note(note_id: str, note_type: NoteType, body: str, confidence: str | None) -> str:
    fm = VaultFrontmatter(
        id=note_id,
        type=note_type,
        discipline=None,
        created=utc_now_iso(),
        source_run=None,
        confidence=confidence,
        status="active",
        supersedes=None,
    )
    return render_frontmatter(fm) + "\n" + body


def _table_link(key: str, title: str) -> str:
    return f"[[{key}\\|{title}]]"


def _has_evidence_from(registry: Registry, bench_key: str, source_key: str, day: str) -> bool:
    """True only when `source_key` actually published a row for `bench_key`
    dated `day` — the measured-evidence test behind the MOC freshness label.

    Checks both tracked-model benchmark entries and watchlist rows (untracked
    models); both carry `source`/`as_of` provenance stamped by registry.build().
    """
    for model in registry.models:
        entry = model.benchmarks.get(bench_key)
        if entry is not None and entry.source == source_key and entry.as_of[:10] == day:
            return True
    return any(
        row.benchmark == bench_key and row.source == source_key and row.as_of[:10] == day
        for row in registry.watchlist
    )


def _model_context(cfg: ModelIntelConfig, registry: Registry, model: ModelEntry) -> dict[str, Any]:
    domain_titles = {d.key: d.title for d in cfg.domains}
    bench_titles = {b.key: b.title for b in cfg.benchmarks}
    ranked_domains = [
        (ranked.index(model.key) + 1, domain_key)
        for domain_key, ranked in registry.top_by_domain.items()
        if model.key in ranked[:TOP_N_BEST_AT]
    ]
    best_domains = [
        {
            "key": domain_key,
            "title": domain_titles.get(domain_key, domain_key),
            "rank": rank,
            "score": model.capabilities[domain_key].score,
        }
        for rank, domain_key in sorted(ranked_domains)
    ]
    return {
        "title": model.title,
        "provider": model.provider,
        "lineage": model.lineage,
        "fusion_agents": [f"`{a}`" for a in model.fusion_agents],
        "pricing": model.pricing.model_dump() if model.pricing else None,
        "measured_latency_ms": model.measured_latency_ms,
        "best_domains": best_domains,
        "capabilities": [
            {
                "key": d.key,
                "title": d.title,
                "score": model.capabilities[d.key].score,
                "basis": model.capabilities[d.key].basis,
            }
            for d in cfg.domains
        ],
        "benchmark_rows": [
            {
                "key": bench_key,
                "title": bench_titles.get(bench_key, bench_key),
                "score": entry.score,
                "metric": entry.metric,
                "as_of": entry.as_of,
                "source": entry.source,
            }
            for bench_key, entry in sorted(model.benchmarks.items())
        ],
    }


def render_all(
    cfg: ModelIntelConfig, registry: Registry, vault_dir: str | None = None
) -> list[str]:
    """Render every model-intel note; returns the absolute paths written."""
    vault = vault_dir or default_vault_dir()
    model_titles = {m.key: m.title for m in registry.models}
    domain_titles = {d.key: d.title for d in cfg.domains}
    bench_titles = {b.key: b.title for b in cfg.benchmarks}
    written: list[str] = []

    for model in registry.models:
        body = render_template(
            "modelintel/model_note.md.j2", **_model_context(cfg, registry, model)
        )
        written.append(
            write_note(
                vault,
                f"models/{model.key}.md",
                _note(model.key, NoteType.SOURCE, body, "medium"),
            )
        )

    for domain in cfg.domains:
        ranking = []
        for model_key in registry.top_by_domain.get(domain.key, []):
            entry = registry.model_entry(model_key)
            if entry is None:
                continue
            cap = entry.capabilities[domain.key]
            ranking.append(
                {"key": model_key, "title": entry.title, "score": cap.score, "basis": cap.basis}
            )
        blend = [
            {
                "key": c.benchmark,
                "title": bench_titles.get(c.benchmark, c.benchmark),
                "weight": c.weight,
            }
            for c in cfg.domain_blend.get(domain.key, [])
        ]
        extra_basis = {
            "speed": "locally measured CLI latency",
        }.get(domain.key)
        body = render_template(
            "modelintel/capability_note.md.j2",
            title=domain.title,
            description=domain.description,
            ranking=ranking,
            blend=blend,
            extra_basis=extra_basis,
        )
        written.append(
            write_note(
                vault,
                f"capabilities/{domain.key}.md",
                _note(domain.key, NoteType.SOURCE, body, "medium"),
            )
        )

    for bench in cfg.benchmarks:
        if bench.metric == "facts":
            continue  # pricing is rendered on model notes, not as a leaderboard
        scored = [
            (b_entry.score, model.key, model.title, b_entry)
            for model in registry.models
            if (b_entry := model.benchmarks.get(bench.key)) is not None
        ]
        standings = [
            {
                "key": model_key,
                "title": title,
                "score": b_entry.score,
                "as_of": b_entry.as_of,
                "source": b_entry.source,
            }
            for _, model_key, title, b_entry in sorted(scored, reverse=True)
        ]
        watchlist = [w for w in registry.watchlist if w.benchmark == bench.key]
        feeds = [
            {"key": domain_key, "title": domain_titles[domain_key]}
            for domain_key, components in cfg.domain_blend.items()
            if any(c.benchmark == bench.key for c in components)
        ]
        body = render_template(
            "modelintel/benchmark_source_note.md.j2",
            title=bench.title,
            url=bench.url,
            description=bench.description,
            metric=bench.metric,
            standings=standings,
            watchlist=watchlist,
            feeds_domains=feeds,
        )
        written.append(
            write_note(
                vault,
                f"benchmarks/sources/{bench.key}.md",
                _note(bench.key, NoteType.BENCHMARK, body, "medium"),
            )
        )

    moc_domains = []
    for domain in cfg.domains:
        ranked = registry.top_by_domain.get(domain.key, [])
        top = [_table_link(k, model_titles.get(k, k)) for k in ranked[:3]] + ["—", "—", "—"]
        moc_domains.append({"key": domain.key, "title": domain.title, "top": top[:3]})
    bench_status = []
    for bench in cfg.benchmarks:
        if bench.metric == "facts":
            status = "pricing/context facts"
        else:
            source_key = bench.key if bench.key in registry.sources else "grok-research"
            source = registry.sources.get(source_key)
            if not source or not source.ok:
                status = "stale/failed"
            elif _has_evidence_from(registry, bench.key, source_key, source.fetched_at[:10]):
                status = f"fresh ({source.fetched_at[:10]})"
            else:
                # The source ran and succeeded, but published NOTHING for this
                # board today (the research sweep is explicitly told to omit a
                # benchmark it cannot verify). A sibling source's success is not
                # a measurement of this board — never report it as fresh.
                status = "no data (source ok, nothing published today)"
        bench_status.append({"key": bench.key, "title": bench.title, "status": status})
    body = render_template(
        "modelintel/moc_note.md.j2",
        updated_at=registry.updated_at,
        domains=moc_domains,
        models=[
            {
                "key": m.key,
                "title": m.title,
                "lineage": m.lineage,
                "fusion_agents": [f"`{a}`" for a in m.fusion_agents],
            }
            for m in registry.models
        ],
        benchmarks=bench_status,
        releases=[r.model_dump() for r in registry.releases],
    )
    written.append(
        write_note(
            vault,
            "sources/model-intelligence.md",
            _note("model-intelligence", NoteType.SOURCE, body, None),
        )
    )
    return written
