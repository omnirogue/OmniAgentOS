#!/usr/bin/env python3
"""memcert MEM-J retention/regression carrier (DESIGN §3 axis J, built in v2).

Axis J asks: is memory quality HOLDING run over run? The certified mechanism
is the UVG idiom — paired per-item deltas between two runs' results, never a
comparison of two independent aggregates. Rows are matched on
``(item_id, trial)`` within an (arm, model) slice and fed to
``grade.paired_delta`` (cluster bootstrap CI + exact McNemar); an axis has
REGRESSED when its paired delta is significantly negative (CI entirely < 0).

Inputs are run directories written by ``run_bench.py``
(``var/memcert/runs/<id>/results.jsonl``); everything is offline re-grading —
zero model calls, so the carrier is hermetic given two result files.

CLI::

    python scripts/memcert/retention.py --prev var/memcert/runs/A \
        --curr var/memcert/runs/B [--arm system] [--model m] [--out PATH]

Exit codes: 0 = no regression; 1 = at least one axis regressed; 2 = usage.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

try:  # imported as a package member: `scripts.memcert.retention`
    from . import grade
except ImportError:  # pragma: no cover - bare-script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import grade  # type: ignore[no-redef]


def load_rows(run_dir: Path) -> list[dict[str, Any]]:
    """results.jsonl rows for a run dir (accepts the file itself, too)."""
    path = run_dir if run_dir.suffix == ".jsonl" else run_dir / "results.jsonl"
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "item_id" in obj and "score" in obj:
            rows.append(obj)
    return rows


def _slice(
    rows: list[dict[str, Any]], arm: str | None, model: str | None
) -> list[dict[str, Any]]:
    out = rows
    if arm is not None:
        out = [r for r in out if r.get("arm") == arm]
    if model is not None:
        out = [r for r in out if r.get("model") == model]
    return out


def _qualified(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows with slice-qualified item ids so pairing NEVER crosses slices.

    ``grade.paired_delta`` matches on ``(item_id, trial)`` alone; in a
    multi-arm/multi-model run the same item id appears once per slice and
    unqualified indexing collapses them last-wins — a stable slice can mask a
    regressed one, and row order changes the verdict (codex-critic CR-001).
    Qualifying the id by (arm, model) makes pairing exact within each slice
    while still pooling every pair for the overall delta.
    """
    return [
        {**r, "item_id": f"{r.get('arm')}|{r.get('model')}|{r.get('item_id')}"}
        for r in rows
    ]


def retention_report(
    prev_rows: list[dict[str, Any]],
    curr_rows: list[dict[str, Any]],
    *,
    arm: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Paired run-over-run deltas, overall and per axis.

    Pairs are matched WITHIN an (arm, model) slice (slice-qualified ids), so a
    stable slice can never mask a regressed one. ``regressed_axes`` lists axes
    whose paired delta CI sits entirely below zero. Slices with zero pairs
    report ``n_pairs: 0`` and never regress (no evidence is not negative
    evidence — but it IS surfaced: the CLI treats ``overall.n_pairs == 0`` as
    VOID, exit 2).
    """
    prev_f = _slice(prev_rows, arm, model)
    curr_f = _slice(curr_rows, arm, model)
    # Slice parity (codex-critic CR-001-R2): a slice present in the previous
    # run but ABSENT from the current one is a broken comparison, not a green
    # one — 8 stable legacy pairs must not paper over a vanished system slice.
    prev_slices = {(r.get("arm"), r.get("model")) for r in prev_f}
    curr_slices = {(r.get("arm"), r.get("model")) for r in curr_f}
    missing_slices = sorted(f"{a}/{m}" for a, m in prev_slices - curr_slices)
    prev = _qualified(prev_f)
    curr = _qualified(curr_f)
    # MEASUREMENT-IDENTITY parity — the structural closure of the whole
    # lost-coverage class (CR-001-R2 slice -> CR-009 axis -> CR-009-R4
    # per-slice axis -> CR-010 item/trial -> CR-010-R5 payload/duplicates).
    # The identity of one measurement is the FULL tuple (slice-qualified
    # item, trial, axis, cluster) — parity checked there subsumes every
    # coarser grain AND catches a key silently moving axes or clusters
    # (which changes what the statistics mean). Ambiguous input refuses:
    # duplicate identities make last-write-wins pairing order-dependent, and
    # a non-finite score is a corrupted measurement, not a zero delta.
    def _identity(r: dict[str, Any]) -> tuple[Any, ...]:
        return (r["item_id"], r.get("trial", 0), str(r.get("axis")), str(r.get("cluster_id")))

    prev_ids = [_identity(r) for r in prev]
    curr_ids = [_identity(r) for r in curr]
    duplicate_keys = (len(prev_ids) - len(set(prev_ids))) + (
        len(curr_ids) - len(set(curr_ids))
    )
    invalid_scores = sum(
        1
        for r in [*prev, *curr]
        if not isinstance(r.get("score"), (int, float)) or not math.isfinite(float(r["score"]))
    )
    lost_keys = sorted(set(prev_ids) - set(curr_ids), key=str)
    axes = sorted({str(r.get("axis")) for r in prev if r.get("axis")})
    per_axis: dict[str, Any] = {}
    regressed: list[str] = []
    void_axes: list[str] = []
    for ax in axes:
        delta = grade.paired_delta(
            [r for r in prev if r.get("axis") == ax],
            [r for r in curr if r.get("axis") == ax],
        )
        per_axis[ax] = delta
        ci_hi = delta.get("ci_hi")
        if delta["n_pairs"] > 0 and ci_hi is not None and ci_hi < 0.0:
            regressed.append(ax)
        elif delta["n_pairs"] == 0:
            # An axis measured previously with ZERO current pairs is a broken
            # comparison at axis grain (codex-critic CR-009): whole-slice
            # parity cannot see it because other axes in the slice still pair.
            void_axes.append(ax)
    overall = grade.paired_delta(prev, curr)
    return {
        "arm": arm,
        "model": model,
        "slices": sorted(f"{a}/{m}" for a, m in curr_slices),
        "missing_slices": missing_slices,
        "void_axes": void_axes,
        "lost_pairs": {
            "count": len(lost_keys),
            "sample": [f"{k[0]}#t{k[1]}" for k in lost_keys[:10]],
        },
        "duplicate_keys": duplicate_keys,
        "invalid_scores": invalid_scores,
        "overall": overall,
        "per_axis": per_axis,
        "regressed_axes": regressed,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="memcert MEM-J retention (paired run-over-run)")
    p.add_argument("--prev", type=Path, required=True, help="previous run dir / results.jsonl")
    p.add_argument("--curr", type=Path, required=True, help="current run dir / results.jsonl")
    p.add_argument("--arm", default=None, help="restrict to one arm")
    p.add_argument("--model", default=None, help="restrict to one model")
    p.add_argument("--out", type=Path, default=None, help="write full JSON report here")
    args = p.parse_args(argv)

    try:
        prev_rows = load_rows(args.prev)
        curr_rows = load_rows(args.curr)
    except OSError as exc:
        print(f"refused: cannot read run rows: {exc}", file=sys.stderr)
        return 2

    report = retention_report(prev_rows, curr_rows, arm=args.arm, model=args.model)
    overall = report["overall"]
    print(
        f"retention pairs={overall['n_pairs']} delta={overall['delta']} "
        f"ci=[{overall['ci_lo']},{overall['ci_hi']}] regressed={report['regressed_axes']}"
    )
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    if overall["n_pairs"] == 0:
        # A comparison that compared NOTHING must never read as green: exit 2
        # (instrument/do-not-retry class), distinct from a real regression
        # (gemini-critic F5, 2026-08-13 — favourable-absence class).
        print("VOID: no matched pairs between the two runs", file=sys.stderr)
        return 2
    if report["missing_slices"]:
        # Same class one level down (codex-critic CR-001-R2): pairs exist, but
        # an entire previously-measured slice vanished from the current run.
        print(
            "VOID: slice(s) missing from the current run: "
            + ", ".join(report["missing_slices"]),
            file=sys.stderr,
        )
        return 2
    if report["void_axes"]:
        # And one level further (CR-009): the slice survived but a previously-
        # measured AXIS inside it has zero current pairs.
        print(
            "VOID: axis(es) with zero current pairs: " + ", ".join(report["void_axes"]),
            file=sys.stderr,
        )
        return 2
    if report["lost_pairs"]["count"]:
        # The structural check (CR-010): ANY previously-measured pair key with
        # no current counterpart voids the comparison, at every grain.
        print(
            f"VOID: {report['lost_pairs']['count']} previously-measured pair key(s) "
            "missing from the current run; sample: "
            + ", ".join(report["lost_pairs"]["sample"]),
            file=sys.stderr,
        )
        return 2
    if report["duplicate_keys"] or report["invalid_scores"]:
        # Ambiguous or corrupted input refuses (CR-010-R5): duplicates make
        # pairing order-dependent; non-finite scores are not measurements.
        print(
            f"VOID: {report['duplicate_keys']} duplicate measurement identit(ies), "
            f"{report['invalid_scores']} non-finite score(s) — input is not a valid "
            "paired comparison",
            file=sys.stderr,
        )
        return 2
    return 1 if report["regressed_axes"] else 0


__all__ = ["load_rows", "retention_report"]

if __name__ == "__main__":
    raise SystemExit(main())
