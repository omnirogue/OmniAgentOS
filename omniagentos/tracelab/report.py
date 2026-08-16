"""Markdown report writer for tracelab runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from omniagentos.tracelab.hypotheses import Hypothesis
from omniagentos.tracelab.mine import MiningResult


def _fmt_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _fmt_count(value: float | None) -> str:
    if value is None:
        raise TypeError("metric summary count must be measured")
    return str(int(value))


def render_report(result: MiningResult, hypotheses: list[Hypothesis]) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    parts: list[str] = [
        "# Tracelab corpus report",
        f"_Generated {now}. Every number below is a mechanical count over parsed traces;"
        " exemplar trace IDs are auditable._",
        "",
        "## Corpus",
        _fmt_table(
            ["dataset", "traces"],
            [[k, str(v)] for k, v in sorted(result.dataset_counts.items())],
        ),
        "",
        f"Total: **{result.n_traces}** traces, **{result.n_labeled}** with ground-truth "
        f"outcome labels ({json.dumps(result.outcome_counts)}).",
        "",
        "## Failure taxonomy (error tool-results only)",
        _fmt_table(
            ["taxon", "count"],
            [[k, str(v)] for k, v in sorted(result.taxon_counts.items(), key=lambda kv: -kv[1])],
        )
        if result.taxon_counts
        else "_No classified tool errors._",
        "",
        "## Metric summary",
        _fmt_table(
            ["metric", "n", "mean", "median", "max"],
            [
                [
                    name,
                    _fmt_count(s["n"]),
                    "—" if s["mean"] is None else str(s["mean"]),
                    "—" if s["median"] is None else str(s["median"]),
                    "—" if s["max"] is None else str(s["max"]),
                ]
                for name, s in result.metric_summary.items()
            ],
        ),
    ]

    if result.contrasts:
        parts += [
            "",
            "## Success vs failure contrasts (labeled traces)",
            _fmt_table(
                ["metric", "success mean", "failure mean", "delta"],
                [
                    [
                        name,
                        str(c["success_mean"]),
                        str(c["failure_mean"]),
                        str(c["delta"]),
                    ]
                    for name, c in result.contrasts.items()
                ],
            ),
        ]

    if result.pattern_lift:
        parts += [
            "",
            "## Anti-pattern prevalence",
            _fmt_table(
                ["pattern", "success prevalence", "failure prevalence"],
                [
                    [
                        name,
                        str(lift.get("success_prevalence", "—")),
                        str(lift.get("failure_prevalence", "—")),
                    ]
                    for name, lift in result.pattern_lift.items()
                ],
            ),
        ]

    parts += ["", f"## Proposed hypotheses ({len(hypotheses)})"]
    if hypotheses:
        for hypothesis in hypotheses:
            parts += [
                "",
                f"### `{hypothesis.hypothesis_id}` — `{hypothesis.independent_variable}`",
                f"_confidence: {hypothesis.confidence}_",
                "",
                hypothesis.statement,
            ]
            exemplars = hypothesis.evidence.get("exemplars")
            if isinstance(exemplars, list) and exemplars:
                parts.append("")
                parts.append("Evidence traces:")
                for row in exemplars:
                    if isinstance(row, dict):
                        parts.append(
                            f"- `{row.get('trace_id', '?')}` ({row.get('dataset', '?')}, "
                            f"{row.get('detail', '')}) — {row.get('source_path', '')}"
                        )
    else:
        parts.append("_No hypothesis cleared its evidence threshold on this corpus._")

    parts.append("")
    return "\n".join(parts)


def write_report(result: MiningResult, hypotheses: list[Hypothesis], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"report-{stamp}.md"
    path.write_text(render_report(result, hypotheses))
    (out_dir / "mining-summary.json").write_text(
        json.dumps(result.to_summary(), indent=2, default=str)
    )
    return path
