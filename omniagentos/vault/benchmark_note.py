"""render_benchmark_note (contracts/interfaces.md §p05).

Aggregates a list of frozen `contracts.RunManifest` (ledger-shaped, not raw
Store rows, so this generator is fully typed unlike render_run_note).
"""

from __future__ import annotations

from collections import Counter

from omniagentos.contracts import NoteType, RunManifest, VaultFrontmatter, utc_now_iso
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.paths import benchmark_relpath
from omniagentos.vault.templating import render_template

UNKNOWN = "_unknown_"
NOT_REPORTED = "not reported"


def render_benchmark_note(bench_id: str, manifests: list[RunManifest]) -> tuple[str, str]:
    """Render a benchmark note. Returns (relpath, full note content). Links
    every run note it aggregates (contract: "Benchmark notes link every run
    note they aggregate") plus `[[Home]]` so the note is never an orphan even
    when `manifests` is empty.
    """
    fm = VaultFrontmatter(
        id=bench_id,
        type=NoteType.BENCHMARK,
        discipline=_common_discipline(manifests),
        created=utc_now_iso(),
        source_run=None,  # a benchmark aggregates many runs, not one
        confidence=None,
        status="active",
        supersedes=None,
    )

    state_counts = Counter(str(m.state) for m in manifests)
    state_summary = (
        ", ".join(f"{state}: {count}" for state, count in sorted(state_counts.items()))
        if state_counts
        else "_none_"
    )

    costs = [m.usage.cost_usd for m in manifests if m.usage is not None and m.usage.cost_usd is not None]
    any_estimated = any(m.usage is None or m.usage.estimated for m in manifests)
    total_cost = f"${sum(costs):.4f} USD" if costs else NOT_REPORTED
    estimated_flag = f"**{'yes' if any_estimated else 'no'}**"
    if manifests:
        estimated_flag += f" ({len(costs)} of {len(manifests)} runs reported cost)"

    rows = [
        {
            "run_id": m.run_id,
            "harness": str(m.harness.harness),
            "arm": str(m.arm) if m.arm is not None else "_none_",
            "model": m.model or UNKNOWN,
            "state": str(m.state),
            "tokens": _tokens_str(m),
            "cost": _cost_str(m),
            "wall_ms": m.usage.wall_ms if m.usage is not None else UNKNOWN,
        }
        for m in manifests
    ]

    body = render_template(
        "benchmark_note.md.j2",
        title=bench_id,
        total=len(manifests),
        state_summary=state_summary,
        total_cost=total_cost,
        estimated_flag=estimated_flag,
        rows=rows,
    )
    relpath = benchmark_relpath(bench_id)
    return relpath, render_frontmatter(fm) + "\n" + body


def _common_discipline(manifests: list[RunManifest]) -> str | None:
    disciplines = {m.discipline for m in manifests if m.discipline is not None}
    if len(disciplines) == 1:
        return next(iter(disciplines))
    return None


def _tokens_str(m: RunManifest) -> str:
    if m.usage is None:
        return UNKNOWN
    inp, out = m.usage.input_tokens, m.usage.output_tokens
    if inp is None and out is None:
        return NOT_REPORTED
    in_str = "?" if inp is None else str(inp)
    out_str = "?" if out is None else str(out)
    return f"{in_str}/{out_str}"


def _cost_str(m: RunManifest) -> str:
    if m.usage is None or m.usage.cost_usd is None:
        return NOT_REPORTED
    return f"${m.usage.cost_usd:.4f}"
