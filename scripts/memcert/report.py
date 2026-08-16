#!/usr/bin/env python3
"""memcert report rendering: SUMMARY.md + junit.xml (DESIGN §12, run_bench brief).

Both functions consume the ``summary`` dict produced by
``scripts/memcert/run_bench.py::run()``:
    summary = {
        "run_id": str,
        "manifest": {...},
        "axes": {
            "<axis>/<arm>/<model>": {
                "axis": str, "arm": str, "model": str,
                "n_rows": int, "n_items": int, "n_trials": int,
                "mean": float | None, "ci_lo": float | None, "ci_hi": float | None,
                "pass_k": bool | None, "verdicts": {verdict: count},
                "cost_usd": float,
            },
            ...
        },
        "parked_pairs": [...],
        "row_count": int,
        "error_count": int,
    }
``summary["axes"]`` is FLAT (keyed by the "axis/arm/model" string) -- this is
``scripts/memcert/grade.py::summarize``'s own shape, which ``run_bench.py``
adopts verbatim (only merging in ``cost_usd``, which grading never computes).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def _fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _by_axis(axes: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in axes.values():
        grouped.setdefault(entry["axis"], []).append(entry)
    for entries in grouped.values():
        entries.sort(key=lambda e: (e["arm"], e["model"]))
    return grouped


def render_summary_md(summary: dict[str, Any]) -> str:
    manifest = summary.get("manifest", {})
    axes = summary.get("axes", {})
    lines: list[str] = []
    lines.append(f"# memcert run `{summary.get('run_id', '?')}`")
    lines.append("")
    lines.append(f"- split: `{manifest.get('split')}`  adapter: `{manifest.get('adapter')}`")
    lines.append(
        f"- seeds: `{manifest.get('seeds')}`  scale: `{manifest.get('scale')}`  "
        f"trials: `{manifest.get('trials')}`  k_trials: `{manifest.get('k_trials')}`"
    )
    lines.append(
        f"- rows: {summary.get('row_count', 0)}  errors: {summary.get('error_count', 0)}  "
        f"total cost: ${_fmt(manifest.get('total_cost_usd'), 4)}  "
        f"wall: {manifest.get('wall_time_ms', 0)}ms"
    )
    lines.append(f"- canary: `{manifest.get('canary')}`")
    if summary.get("parked_pairs"):
        lines.append(f"- **parked (terminal errors):** `{', '.join(summary['parked_pairs'])}`")
    lines.append("")

    grouped = _by_axis(axes)
    if not grouped:
        lines.append("_No graded cells (instrument failure or empty run)._")
        return "\n".join(lines) + "\n"

    for axis in sorted(grouped):
        lines.append(f"## MEM-{axis}")
        lines.append("")
        lines.append("| Arm | Model | Mean | 95% CI | n_rows | n_items | pass_k | Verdicts | Cost $ |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for entry in grouped[axis]:
            ci = f"[{_fmt(entry['ci_lo'])}, {_fmt(entry['ci_hi'])}]"
            verdicts = ", ".join(f"{k}={v}" for k, v in sorted(entry["verdicts"].items()))
            pass_k = entry["pass_k"]
            pass_str = "n/a" if pass_k is None else ("PASS" if pass_k else "FAIL")
            lines.append(
                f"| {entry['arm']} | {entry['model']} | {_fmt(entry['mean'])} | {ci} | "
                f"{entry['n_rows']} | {entry['n_items']} | {pass_str} | {verdicts} | "
                f"{_fmt(entry.get('cost_usd'), 4)} |"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def write_junit(
    summary: dict[str, Any],
    path: str | Path,
    bars: dict[str, float] | None = None,
) -> None:
    """One <testcase name="MEM-<axis>--<arm>--<model>"> per graded (axis,arm,model) cell.

    A cell fails only when ``bars`` names its axis, the cell has a mean, AND
    that mean falls below the bar — with no bars passed (or a cell with no
    mean, e.g. zero graded rows), every testcase is a bare pass: dev runs are
    informational, not gating. junit schema kept pytest-compatible:
    <testsuites><testsuite tests= failures= errors= skipped=><testcase .../></testsuite></testsuites>
    """
    axes = summary.get("axes", {})
    testcases: list[ET.Element] = []
    failures = 0
    for key in sorted(axes):
        entry = axes[key]
        name = f"MEM-{entry['axis']}--{entry['arm']}--{entry['model']}"
        tc = ET.Element(
            "testcase",
            {
                "classname": "memcert",
                "name": name,
                "time": f"{0.0:.3f}",
            },
        )
        bar = (bars or {}).get(entry["axis"])
        mean = entry.get("mean")
        # Sol review MC-002/MC-003 + Grok SF-3: a cell fails on mean-below-bar,
        # a MEASURED pass^k False, error degradation, or degenerate abstention
        # — the junit carrier and the exit code must tell the same story.
        fail_reasons = []
        if bar is not None and mean is not None and mean < bar:
            fail_reasons.append(f"mean {mean:.4f} < bar {bar:.4f}")
        if bar is not None and entry.get("pass_k") is False:
            fail_reasons.append("pass^k False (a trial fell below the bar)")
        if bar is not None and entry.get("error_degraded"):
            fail_reasons.append(">5% of the cell's calls errored")
        if bar is not None and entry.get("degenerate_abstain"):
            fail_reasons.append(">90% abstention on a non-E axis")
        if fail_reasons:
            failures += 1
            failure = ET.SubElement(
                tc,
                "failure",
                {"message": "; ".join(fail_reasons)},
            )
            ci_lo, ci_hi = entry.get("ci_lo"), entry.get("ci_hi")
            failure.text = (
                f"axis={entry['axis']} arm={entry['arm']} model={entry['model']} "
                f"mean={_fmt(mean, 4)} ci=[{_fmt(ci_lo, 4)},{_fmt(ci_hi, 4)}] "
                f"n_rows={entry['n_rows']} bar={_fmt(bar, 4)}"
            )
        testcases.append(tc)

    suite = ET.Element(
        "testsuite",
        {
            "name": "memcert",
            "tests": str(len(testcases)),
            "failures": str(failures),
            "errors": "0",
            "skipped": "0",
        },
    )
    for tc in testcases:
        suite.append(tc)
    root = ET.Element("testsuites")
    root.append(suite)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    ET.ElementTree(root).write(tmp, encoding="unicode", xml_declaration=True)
    tmp.replace(path)
