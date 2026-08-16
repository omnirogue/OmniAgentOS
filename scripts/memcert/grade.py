"""memcert grade: aggregation + statistics over benchmark result rows.

Design of record: devtasks/memcert/DESIGN.md (§6 scoring, §12 contracts).
Per-item scoring lives in ``core.grade_item`` — this module never rescores an
answer itself; it applies core's algebra to raw rows and aggregates:

- ``grade_rows``:    enrich raw result rows with (verdict, score) via core.grade_item.
- ``summarize``:     per (axis, arm, model) mean + cluster bootstrap CI95 + pass^k.
- ``paired_delta``:  paired per-item A/B delta with cluster bootstrap CI and an
  exact McNemar test on the binarized discordant pairs (DESIGN §6: A/B deltas
  are judged ONLY by paired per-item differences).
- ``mde_hint``:      rough minimum-detectable-effect hint for planning trial counts.

Statistics notes:
- Clustering unit is ``cluster_id`` (the fixture world). Items sharing a history
  are not independent, so bootstrap resampling draws CLUSTERS with replacement
  (Anthropic arXiv:2411.00640), never individual item-trials.
- All randomness comes from ``core.rng_for(boot_seed, <stable-name>)`` so every
  call with the same inputs reproduces the same CI bit-for-bit.
- All floats in outputs are rounded to 4 decimals; degenerate inputs (no rows,
  no pairs, n<=0) yield ``None`` entries, never a ZeroDivisionError.

CLI (optional convenience — re-grade a run dir from an answers export):
    python -m scripts.memcert.grade --items <answers.json> --rows <results.jsonl>
        [--bars <bars.json> --k 3] [--boot-seed 1] [--n-boot 2000]
``answers.json`` is the protected-store export (items WITH answer_spec); fixture
dirs written by ``gen.py`` never contain answer specs (DESIGN §4).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:  # imported as a package member: `scripts.memcert.grade`
    from .core import AnswerSpec, Item, grade_item, rng_for
except ImportError:
    # Run as a SCRIPT (e.g. via Makefile): sys.path[0] is this directory and
    # there is no parent package for the relative import to resolve against.
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from core import AnswerSpec, Item, grade_item, rng_for

__all__ = [
    "grade_rows",
    "summarize",
    "paired_delta",
    "mcnemar_exact",
    "mde_hint",
    "main",
]


def _r4(x: float | None) -> float | None:
    return None if x is None else round(float(x), 4)


# ---------------------------------------------------------------------------
# grading


def grade_rows(items_by_id: dict[str, Item], raw_rows: list[dict]) -> list[dict]:
    """Apply ``core.grade_item`` to each row's ``raw_answer``.

    Returns NEW row dicts (inputs are not mutated) with ``verdict`` and
    ``score`` set, and ``axis`` / ``level`` / ``cluster_id`` filled in from the
    item when the row does not already carry them (summarize/paired_delta need
    ``cluster_id`` for clustering). A row referencing an unknown item_id is a
    harness bug and raises KeyError — silently skipping would bias every mean.
    """
    out: list[dict] = []
    for row in raw_rows:
        item_id = row.get("item_id")
        item = items_by_id.get(item_id) if item_id is not None else None
        if item is None:
            raise KeyError(f"result row references unknown item_id: {item_id!r}")
        verdict, score = grade_item(item.answer_spec, row.get("raw_answer") or "")
        enriched = dict(row)
        enriched["verdict"] = verdict
        enriched["score"] = score
        enriched.setdefault("axis", item.axis)
        enriched.setdefault("level", item.level)
        enriched.setdefault("cluster_id", item.cluster_id)
        out.append(enriched)
    return out


# ---------------------------------------------------------------------------
# bootstrap machinery


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    """Linear-interpolated percentile over an already-sorted list."""
    if not sorted_vals:
        return None
    idx = q * (len(sorted_vals) - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_vals[lo]
    frac = idx - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _cluster_bootstrap_ci(
    values_by_cluster: dict[str, list[float]],
    rng: Any,
    n_boot: int,
) -> tuple[float | None, float | None]:
    """CI95 of the mean under cluster resampling.

    Draw len(clusters) cluster ids WITH replacement; each bootstrap statistic is
    the mean of every item-trial score belonging to the sampled clusters (with
    multiplicity). Percentile interval at 2.5/97.5.
    """
    clusters = sorted(values_by_cluster)
    if not clusters or n_boot <= 0:
        return (None, None)
    n = len(clusters)
    stats: list[float] = []
    for _ in range(n_boot):
        picked = [clusters[rng.randrange(n)] for _ in range(n)]
        vals = [v for c in picked for v in values_by_cluster[c]]
        # vals is never empty: every cluster key holds at least one score.
        stats.append(sum(vals) / len(vals))
    stats.sort()
    return (_percentile(stats, 0.025), _percentile(stats, 0.975))


# ---------------------------------------------------------------------------
# aggregation


def summarize(
    rows: list[dict],
    bars: dict[str, float] | None = None,
    k: int | None = None,
    boot_seed: int = 1,
    n_boot: int = 2000,
) -> dict[str, dict[str, Any]]:
    """Aggregate graded rows per (axis, arm, model).

    Returns ``{"<axis>/<arm>/<model>": entry}`` where entry carries:
    ``axis, arm, model, n_rows, n_items, n_trials, mean, ci_lo, ci_hi,
    pass_k, verdicts``.

    - ``mean`` is over all item-trial scores in the group.
    - CI95 is a cluster bootstrap over ``cluster_id`` (see module docstring),
      deterministic for a fixed ``boot_seed``.
    - ``pass_k`` (DESIGN §6 pass^k): when BOTH ``bars`` and ``k`` are given and
      the axis has a bar, True iff at least k trials were observed AND EVERY
      trial's per-trial mean >= bars[axis]; otherwise ``None`` (not evaluable).
    - ``verdicts`` is a per-verdict count dict.

    Empty ``rows`` -> ``{}``; a group can never be empty, but all float fields
    degrade to None rather than dividing by zero.
    """
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        key = (str(row.get("axis", "")), str(row.get("arm", "")), str(row.get("model", "")))
        groups[key].append(row)

    out: dict[str, dict[str, Any]] = {}
    for axis, arm, model in sorted(groups):
        grp = groups[(axis, arm, model)]
        scores = [float(r["score"]) for r in grp]
        mean = sum(scores) / len(scores) if scores else None

        by_cluster: dict[str, list[float]] = defaultdict(list)
        for r in grp:
            by_cluster[str(r.get("cluster_id", ""))].append(float(r["score"]))
        rng = rng_for(boot_seed, f"summarize:{axis}:{arm}:{model}")
        ci_lo, ci_hi = _cluster_bootstrap_ci(by_cluster, rng, n_boot)

        by_trial: dict[Any, list[float]] = defaultdict(list)
        for r in grp:
            by_trial[r.get("trial", 0)].append(float(r["score"]))
        trial_means = {t: sum(v) / len(v) for t, v in by_trial.items()}

        pass_k: bool | None = None
        if bars is not None and k is not None and axis in bars:
            bar = float(bars[axis])
            # Sol review MC-002: fewer trials than k means pass^k was NOT
            # MEASURED (None), never False — a 1-trial dev run must not read
            # as a reliability failure, and a k-trial run must actually gate.
            if len(trial_means) >= k:
                pass_k = all(tm >= bar for tm in trial_means.values())

        out[f"{axis}/{arm}/{model}"] = {
            "axis": axis,
            "arm": arm,
            "model": model,
            "n_rows": len(grp),
            "n_items": len({r.get("item_id") for r in grp}),
            "n_trials": len(trial_means),
            "mean": _r4(mean),
            "ci_lo": _r4(ci_lo),
            "ci_hi": _r4(ci_hi),
            "pass_k": pass_k,
            "verdicts": dict(Counter(str(r.get("verdict", "")) for r in grp)),
        }
    return out


# ---------------------------------------------------------------------------
# paired statistics


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the two discordant counts.

    p = 2 * sum_{i<=min(b,c)} C(b+c, i) * 0.5^(b+c), capped at 1.0.
    b+c == 0 (no discordant pairs) -> 1.0 by convention.
    """
    n = b + c
    if n == 0:
        return 1.0
    m = min(b, c)
    p = 2.0 * sum(math.comb(n, i) for i in range(m + 1)) * (0.5**n)
    return min(p, 1.0)


def paired_delta(
    rows_a: list[dict],
    rows_b: list[dict],
    boot_seed: int = 1,
    n_boot: int = 2000,
) -> dict[str, Any]:
    """Paired per-item delta between two arms/runs over the SAME items.

    Rows are matched on ``(item_id, trial)``; unmatched rows are ignored.
    delta = mean over pairs of (score_b - score_a) — positive means B beats A.
    CI95 is a cluster bootstrap on the per-pair deltas (clustered by
    ``cluster_id``); ``significant`` = the CI excludes 0.

    McNemar (exact, two-sided) runs on the binarized outcomes
    (success := score == 1.0): ``mcnemar_b`` counts pairs where only A
    succeeded, ``mcnemar_c`` pairs where only B succeeded.

    Returns ``{n_pairs, delta, ci_lo, ci_hi, significant, mcnemar_b,
    mcnemar_c, mcnemar_p}``; zero pairs yields None floats, never a crash.
    """

    def index(rows: list[dict]) -> dict[tuple[Any, Any], dict]:
        return {(r["item_id"], r.get("trial", 0)): r for r in rows}

    ia = index(rows_a)
    ib = index(rows_b)
    keys = sorted(set(ia) & set(ib), key=str)
    n_pairs = len(keys)
    if n_pairs == 0:
        return {
            "n_pairs": 0,
            "delta": None,
            "ci_lo": None,
            "ci_hi": None,
            "significant": False,
            "mcnemar_b": 0,
            "mcnemar_c": 0,
            "mcnemar_p": None,
        }

    deltas_by_cluster: dict[str, list[float]] = defaultdict(list)
    deltas: list[float] = []
    b_cnt = 0
    c_cnt = 0
    for key in keys:
        ra, rb = ia[key], ib[key]
        d = float(rb["score"]) - float(ra["score"])
        deltas.append(d)
        cluster = str(rb.get("cluster_id") or ra.get("cluster_id") or "")
        deltas_by_cluster[cluster].append(d)
        a_win = float(ra["score"]) == 1.0
        b_win = float(rb["score"]) == 1.0
        if a_win and not b_win:
            b_cnt += 1
        elif b_win and not a_win:
            c_cnt += 1

    delta = sum(deltas) / n_pairs
    rng = rng_for(boot_seed, "paired_delta")
    ci_lo, ci_hi = _cluster_bootstrap_ci(deltas_by_cluster, rng, n_boot)
    significant = ci_lo is not None and ci_hi is not None and (ci_lo > 0.0 or ci_hi < 0.0)
    return {
        "n_pairs": n_pairs,
        "delta": _r4(delta),
        "ci_lo": _r4(ci_lo),
        "ci_hi": _r4(ci_hi),
        "significant": significant,
        "mcnemar_b": b_cnt,
        "mcnemar_c": c_cnt,
        "mcnemar_p": _r4(mcnemar_exact(b_cnt, c_cnt)),
    }


def mde_hint(n_items: int, sd: float) -> float | None:
    """Rough minimum detectable effect for a paired design — A HINT ONLY.

    Approximation at 80% power / alpha .05: 2.8 * sd / sqrt(n). It ignores
    clustering, non-normality, and the bootstrap actually used for inference —
    use it to size dev-split runs, never to declare significance.
    Returns None when n_items <= 0 (nothing detectable from nothing).
    """
    if n_items <= 0:
        return None
    return _r4(2.8 * float(sd) / math.sqrt(n_items))


# ---------------------------------------------------------------------------
# CLI (re-grade a run dir from an answers export)


def _item_from_json(d: dict[str, Any]) -> Item:
    return Item(
        item_id=d["item_id"],
        axis=d["axis"],
        level=int(d.get("level", 1)),
        split=d.get("split", "dev"),
        question=d.get("question", ""),
        answer_spec=AnswerSpec.from_json(d["answer_spec"]),
        session_scope=tuple(d.get("session_scope", ())),
        cluster_id=d.get("cluster_id", ""),
        arm_overrides=d.get("arm_overrides", {}) or {},
    )


def _load_items(path: Path) -> dict[str, Item]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"items file must be a JSON list of item objects: {path}")
    items = [_item_from_json(d) for d in data]
    return {it.item_id: it for it in items}


def _load_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="memcert-grade",
        description="Re-grade memcert result rows and print the per-(axis,arm,model) summary.",
    )
    ap.add_argument("--items", required=True, type=Path, help="answers export JSON (WITH specs)")
    ap.add_argument("--rows", required=True, type=Path, help="results.jsonl from a run dir")
    ap.add_argument("--bars", type=Path, default=None, help="JSON {axis: bar} for pass^k")
    ap.add_argument("--k", type=int, default=None, help="trials required for pass^k")
    ap.add_argument("--boot-seed", type=int, default=1)
    ap.add_argument("--n-boot", type=int, default=2000)
    args = ap.parse_args(argv)

    items_by_id = _load_items(args.items)
    raw_rows = _load_rows(args.rows)
    graded = grade_rows(items_by_id, raw_rows)
    bars = json.loads(args.bars.read_text(encoding="utf-8")) if args.bars else None
    summary = summarize(graded, bars=bars, k=args.k, boot_seed=args.boot_seed, n_boot=args.n_boot)
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
