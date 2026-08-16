"""Pre-registered analysis for a Hypothesis Tester experiment (DESIGN.md §5-6).

Everything is recomputed from episodes.jsonl (the audit trail), never from
in-memory state, so analysis is reproducible byte-for-byte from the logs.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from .memory import ARMS, est_tokens
from .runner import MAX_EPISODE_ERRORS

BOOT_N = 10_000
INSTRUMENT_STALE_MSG = (
    "refusing to analyze {exp}: instrument code_sha {cur} != {rec} recorded at run "
    "time; pass allow_stale_instrument=True (CLI: --allow-stale-instrument) to "
    "proceed with the mismatch stamped into analysis.json and the ledger"
)


def instrument_code_sha() -> str:
    """Content hash of the instrument itself (every .py in this package)."""
    h = hashlib.sha256()
    for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


BOOT_SEED = 20260812
LATE_START = 9  # episodes 9..16 are the late window


def _load_episodes(exp_dir: Path) -> list[dict[str, Any]]:
    lines = []
    path = exp_dir / "episodes.jsonl"
    # errors="replace": a tail torn inside a multibyte char must not make the
    # whole log unreadable (sibling of the runner's HT-003 repair).
    for raw in path.read_bytes().decode("utf-8", errors="replace").splitlines():
        if raw.strip():
            try:
                lines.append(json.loads(raw))
            except ValueError:
                continue
    return lines


def _latest_gen_by_run(exp_dir: Path) -> dict[str, dict[int, dict[str, Any]]]:
    """Episodes of each run's newest generation; older gens are trail only."""
    by_gen: dict[str, dict[int, dict[int, dict[str, Any]]]] = defaultdict(lambda: defaultdict(dict))
    for line in _load_episodes(exp_dir):
        by_gen[line["run_id"]][int(line.get("gen", 1))][line["ep"]] = line
    return {run_id: gens[max(gens)] for run_id, gens in by_gen.items()}


def run_metrics(exp_dir: Path, episodes_per_run: int = 16) -> dict[str, dict[str, Any]]:
    """Recompute per-run metrics from the episode log (latest generation only)."""
    out: dict[str, dict[str, Any]] = {}
    for run_id, eps in _latest_gen_by_run(exp_dir).items():
        if len(eps) < episodes_per_run:
            continue  # incomplete run: not analyzable yet
        ordered = [eps[i] for i in sorted(eps)]
        errors = sum(1 for e in ordered if e["code"] == "ERROR")
        valid = [e for e in ordered if e["code"] != "ERROR"]
        late = [e for e in valid if e["ep"] >= LATE_START]
        first = ordered[0]
        out[run_id] = {
            "family": first["family"],
            "seed": first["seed"],
            "arm": first["arm"],
            "model": first["model"],
            "errors": errors,
            "invalid": errors > MAX_EPISODE_ERRORS,
            "success": mean(1.0 if e["win"] else 0.0 for e in valid) if valid else None,
            "late_success": mean(1.0 if e["win"] else 0.0 for e in late) if late else None,
            "repeat_rate": mean(1.0 if e.get("repeat_failure") else 0.0 for e in valid)
            if valid
            else None,
            "mem_tokens_mean": mean(est_tokens(e.get("memory_text") or "") for e in ordered),
            "cost_usd": sum(e.get("cost_usd") or 0.0 for e in ordered),
            "prompt_tokens": sum(e["usage"]["prompt_tokens"] for e in ordered),
            "completion_tokens": sum(e["usage"]["completion_tokens"] for e in ordered),
        }
    return out


def bootstrap_ci(values: list[float], n: int = BOOT_N) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "ci_low": 0.0, "ci_high": 0.0, "n": 0}
    rng = random.Random(BOOT_SEED)
    means = []
    for _ in range(n):
        sample = [values[rng.randrange(len(values))] for _ in values]
        means.append(mean(sample))
    means.sort()
    return {
        "mean": round(mean(values), 4),
        "ci_low": round(means[int(0.025 * n)], 4),
        "ci_high": round(means[int(0.975 * n)] if int(0.975 * n) < n else means[-1], 4),
        "n": len(values),
    }


def _eligible(r: dict[str, Any], metric: str = "late_success") -> bool:
    """THE pairing-eligibility rule. token_ratio and paired_deltas must agree,
    or a run excluded from the accuracy contrast can still skew the token
    ratio (HT-001b round 3)."""
    return not r["invalid"] and r[metric] is not None


def paired_deltas(
    runs: dict[str, dict[str, Any]],
    arm: str,
    baseline: str = "none",
    metric: str = "late_success",
    families: list[str] | None = None,
    negate: bool = False,
) -> list[float]:
    """Paired (family, seed, model) deltas arm - baseline on `metric`."""
    idx: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    for r in runs.values():
        if _eligible(r, metric):
            idx[(r["family"], r["seed"], r["model"], r["arm"])] = r
    deltas = []
    for (fam, seed, model, a), r in idx.items():
        if a != arm or (families and fam not in families):
            continue
        b = idx.get((fam, seed, model, baseline))
        if b is None:
            continue
        d = r[metric] - b[metric]
        deltas.append(-d if negate else d)
    return deltas


def token_ratio(runs: dict[str, dict[str, Any]], arm: str, baseline: str = "transcripts") -> float:
    """Memory-token ratio over PAIRED (family, seed, model) runs only.

    Unpaired inclusion let missing treatment data skew the ratio favorably
    (HT-001 reopened): a run counted in one arm's mean with no counterpart in
    the other's. Pairs below the pooled minimum return NaN, which the verdict
    logic maps to INCONCLUSIVE.
    """
    idx: dict[tuple[str, int, str, str], dict[str, Any]] = {
        (r["family"], r["seed"], r["model"], r["arm"]): r for r in runs.values() if _eligible(r)
    }
    pairs = []
    for (fam, seed, model, a), r in idx.items():
        if a != arm:
            continue
        b = idx.get((fam, seed, model, baseline))
        if b is not None:
            pairs.append((r["mem_tokens_mean"], b["mem_tokens_mean"]))
    if len(pairs) < MIN_N_POOLED:
        return float("nan")
    den = mean(p[1] for p in pairs)
    if den == 0:
        return float("nan")
    return round(mean(p[0] for p in pairs) / den, 3)


# A contrast may not CONFIRM or REFUTE on a depleted pair count: the registered
# design has 12 pairs per family contrast and 36 pooled, and silently-skipped
# invalid runs must degrade the verdict, never strengthen it (HT-001).
MIN_N_FAMILY = 8
MIN_N_POOLED = 24


def _verdict_gain(stat: dict[str, float], threshold: float, min_n: int) -> str:
    if stat["n"] < min_n:
        return "INCONCLUSIVE"
    if stat["mean"] >= threshold and stat["ci_low"] > 0:
        return "CONFIRMED"
    if stat["mean"] <= 0:
        return "REFUTED"
    return "INCONCLUSIVE"


def hypotheses(runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    h: dict[str, Any] = {}

    h1 = bootstrap_ci(paired_deltas(runs, "outcomes", families=["strategy"]))
    h["H1"] = {"stat": h1, "verdict": _verdict_gain(h1, 0.10, MIN_N_FAMILY)}

    h2_parts = {}
    for fam in ("gatekeeper", "trapcli"):
        s = bootstrap_ci(paired_deltas(runs, "transcripts", families=[fam]))
        h2_parts[fam] = {"stat": s, "verdict": _verdict_gain(s, 0.10, MIN_N_FAMILY)}
    verdicts = [p["verdict"] for p in h2_parts.values()]
    h["H2"] = {
        "parts": h2_parts,
        "verdict": "CONFIRMED"
        if all(v == "CONFIRMED" for v in verdicts)
        else ("REFUTED" if all(v == "REFUTED" for v in verdicts) else "INCONCLUSIVE"),
    }

    h2b_parts = {}
    for fam in ("gatekeeper", "trapcli"):
        s = bootstrap_ci(paired_deltas(runs, "lessons", families=[fam]))
        h2b_parts[fam] = {"stat": s, "verdict": _verdict_gain(s, 0.10, MIN_N_FAMILY)}
    v2b = [p["verdict"] for p in h2b_parts.values()]
    h["H2b"] = {
        "parts": h2b_parts,
        "verdict": "CONFIRMED"
        if all(v == "CONFIRMED" for v in v2b)
        else ("REFUTED" if all(v == "REFUTED" for v in v2b) else "INCONCLUSIVE"),
    }

    h3_acc = bootstrap_ci(paired_deltas(runs, "lessons", baseline="transcripts"))
    h3_ratio = token_ratio(runs, "lessons")
    if h3_acc["n"] < MIN_N_POOLED or math.isnan(h3_ratio):
        h3_v = "INCONCLUSIVE"
    elif h3_acc["mean"] >= -0.05 and h3_ratio <= 0.50:
        h3_v = "CONFIRMED"
    elif h3_ratio > 0.50 or h3_acc["ci_high"] < -0.05:
        h3_v = "REFUTED"
    else:
        h3_v = "INCONCLUSIVE"
    h["H3"] = {"stat": h3_acc, "token_ratio": h3_ratio, "verdict": h3_v}

    h4 = bootstrap_ci(paired_deltas(runs, "transcripts", baseline="placebo"))
    h["H4"] = {"stat": h4, "verdict": _verdict_gain(h4, 0.10, MIN_N_POOLED)}

    h5_acc = bootstrap_ci(paired_deltas(runs, "activation", baseline="transcripts"))
    h5_ratio = token_ratio(runs, "activation")
    if h5_acc["n"] < MIN_N_POOLED or math.isnan(h5_ratio):
        h5_v = "INCONCLUSIVE"
    elif h5_acc["mean"] >= -0.05 and h5_ratio <= 0.60:
        h5_v = "CONFIRMED"
    elif h5_ratio > 0.60 or h5_acc["ci_high"] < -0.05:
        h5_v = "REFUTED"
    else:
        h5_v = "INCONCLUSIVE"
    h["H5"] = {"stat": h5_acc, "token_ratio": h5_ratio, "verdict": h5_v}

    h6_parts = {}
    for arm in ("transcripts", "lessons"):
        s = bootstrap_ci(paired_deltas(runs, arm, metric="repeat_rate", negate=True))
        h6_parts[arm] = {
            "stat": s,
            "verdict": "CONFIRMED"
            if s["n"] >= MIN_N_POOLED and s["ci_low"] > 0
            else ("REFUTED" if s["n"] >= MIN_N_POOLED and s["mean"] <= 0 else "INCONCLUSIVE"),
        }
    v6 = [p["verdict"] for p in h6_parts.values()]
    h["H6"] = {
        "parts": h6_parts,
        "verdict": "CONFIRMED"
        if all(v == "CONFIRMED" for v in v6)
        else ("REFUTED" if all(v == "REFUTED" for v in v6) else "INCONCLUSIVE"),
    }

    # Amended rule (DESIGN.md §10): experience access may prove out through
    # either the raw-transcript arm or the consolidated-lessons arm.
    exp_ok = "CONFIRMED" in (h["H2"]["verdict"], h["H2b"]["verdict"])
    exp_dead = h["H2"]["verdict"] == h["H2b"]["verdict"] == "REFUTED"
    core = (
        "SUPPORTED"
        if exp_ok and h["H4"]["verdict"] == "CONFIRMED"
        else (
            "REFUTED-AT-THIS-SCALE"
            if h["H4"]["verdict"] == "REFUTED" or exp_dead
            else "INCONCLUSIVE"
        )
    )
    h["core_hypothesis"] = core
    return h


def learning_curves(exp_dir: Path) -> dict[str, dict[str, list[float | None]]]:
    acc: dict[tuple[str, str], dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    max_ep = 0
    for eps in _latest_gen_by_run(exp_dir).values():
        for line in eps.values():
            if line["code"] == "ERROR":
                continue
            acc[(line["family"], line["arm"])][line["ep"]].append(1.0 if line["win"] else 0.0)
            max_ep = max(max_ep, line["ep"])
    out: dict[str, dict[str, list[float | None]]] = defaultdict(dict)
    for (fam, arm), by_ep in acc.items():
        out[fam][arm] = [
            round(mean(by_ep[ep]), 3) if by_ep.get(ep) else None for ep in range(1, max_ep + 1)
        ]
    return dict(out)


def _fmt_stat(s: dict[str, float]) -> str:
    return f"{s['mean']:+.3f} [{s['ci_low']:+.3f}, {s['ci_high']:+.3f}] (n={s['n']})"


def analyze(
    exp_dir: Path, episodes_per_run: int = 16, allow_stale_instrument: bool = False
) -> dict[str, Any]:
    config = json.loads((exp_dir / "config.json").read_text())
    current = instrument_code_sha()
    recorded = config.get("code_sha")
    if recorded != current and not allow_stale_instrument:
        raise RuntimeError(
            INSTRUMENT_STALE_MSG.format(exp=config.get("exp_id"), cur=current, rec=recorded)
        )
    runs = run_metrics(exp_dir, episodes_per_run)
    h = hypotheses(runs)
    curves = learning_curves(exp_dir)

    families = sorted({r["family"] for r in runs.values()})
    arms = [a for a in ARMS if any(r["arm"] == a for r in runs.values())]
    table: dict[str, dict[str, float | None]] = {}
    for fam in families:
        table[fam] = {}
        for arm in arms:
            vals = [
                r["late_success"]
                for r in runs.values()
                if r["family"] == fam
                and r["arm"] == arm
                and not r["invalid"]
                and r["late_success"] is not None
            ]
            table[fam][arm] = round(mean(vals), 3) if vals else None

    total_cost = round(sum(r["cost_usd"] for r in runs.values()), 4)
    invalid = sorted(rid for rid, r in runs.items() if r["invalid"])
    result = {
        "exp_id": config["exp_id"],
        "config_sha": config["config_sha"],
        "analyzed_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "analyzed_with_code_sha": current,
        "run_code_sha": config.get("code_sha"),
        "runs_analyzed": len(runs),
        "runs_invalid": invalid,
        "total_cost_usd": total_cost,
        "late_success_by_family_arm": table,
        "hypotheses": h,
        "learning_curves": curves,
    }
    (exp_dir / "analysis.json").write_text(json.dumps(result, indent=2, sort_keys=True))
    (exp_dir / "ANALYSIS.md").write_text(_render_md(result))
    _append_ledger(exp_dir.parent.parent, result)
    return result


def _render_md(res: dict[str, Any]) -> str:
    lines = [
        "# Hypothesis Tester — analysis (generated; do not hand-edit)",
        "",
        f"experiment `{res['exp_id']}` · config sha `{res['config_sha']}` · "
        f"analyzed {res['analyzed_at']} · {res['runs_analyzed']} runs · "
        f"${res['total_cost_usd']} total",
        "",
        "## Late-window success (episodes 9-16), mean across seeds x models",
        "",
    ]
    fams = sorted(res["late_success_by_family_arm"])
    arms = list(next(iter(res["late_success_by_family_arm"].values())).keys())
    lines.append("| family | " + " | ".join(arms) + " |")
    lines.append("|---" * (len(arms) + 1) + "|")
    for fam in fams:
        row = res["late_success_by_family_arm"][fam]
        cells = " | ".join("-" if row[a] is None else f"{row[a]:.3f}" for a in arms)
        lines.append(f"| {fam} | {cells} |")
    lines += ["", "## Pre-registered hypotheses (DESIGN.md §6)", ""]
    h = res["hypotheses"]
    lines.append(
        f"- **H1** outcomes teach policy (strategy): {_fmt_stat(h['H1']['stat'])} → "
        f"**{h['H1']['verdict']}**"
    )
    for fam, part in h["H2"]["parts"].items():
        lines.append(
            f"- **H2/{fam}** transcripts > none: {_fmt_stat(part['stat'])} → {part['verdict']}"
        )
    lines.append(f"- **H2** overall → **{h['H2']['verdict']}**")
    for fam, part in h["H2b"]["parts"].items():
        lines.append(
            f"- **H2b/{fam}** lessons > none: {_fmt_stat(part['stat'])} → {part['verdict']}"
        )
    lines.append(f"- **H2b** overall → **{h['H2b']['verdict']}**")
    lines.append(
        f"- **H3** lessons ≈ transcripts at ≤50% tokens: Δacc {_fmt_stat(h['H3']['stat'])}, "
        f"token ratio {h['H3']['token_ratio']} → **{h['H3']['verdict']}**"
    )
    lines.append(
        f"- **H4** transcripts > placebo (learning vs priming): "
        f"{_fmt_stat(h['H4']['stat'])} → **{h['H4']['verdict']}**"
    )
    lines.append(
        f"- **H5** activation ≈ transcripts at ≤60% tokens: Δacc {_fmt_stat(h['H5']['stat'])}, "
        f"token ratio {h['H5']['token_ratio']} → **{h['H5']['verdict']}**"
    )
    for arm, part in h["H6"]["parts"].items():
        lines.append(
            f"- **H6/{arm}** repeat-failure inhibition: {_fmt_stat(part['stat'])} → "
            f"{part['verdict']}"
        )
    lines.append(f"- **H6** overall → **{h['H6']['verdict']}**")
    lines += ["", f"## Core hypothesis (theory doc §6): **{h['core_hypothesis']}**", ""]
    if res["runs_invalid"]:
        lines += ["## Excluded runs (>2 API errors)", ""]
        lines += [f"- {rid}" for rid in res["runs_invalid"]]
        lines.append("")
    lines += ["## Learning curves (mean success by episode, pooled seeds x models)", ""]
    for fam, by_arm in sorted(res["learning_curves"].items()):
        lines.append(f"### {fam}")
        for arm, curve in sorted(by_arm.items()):
            cells = " ".join("-" if v is None else f"{v:.2f}" for v in curve)
            lines.append(f"- `{arm:11s}` {cells}")
        lines.append("")
    return "\n".join(lines)


def _append_ledger(var_dir: Path, res: dict[str, Any]) -> None:
    stamp = dt.datetime.now(dt.UTC)
    path = var_dir / f"ledger-{stamp:%Y%m}.jsonl"
    entry = {
        "ts": res["analyzed_at"],
        "exp_id": res["exp_id"],
        "config_sha": res["config_sha"],
        "runs": res["runs_analyzed"],
        "cost_usd": res["total_cost_usd"],
        "run_code_sha": res.get("run_code_sha"),
        "analyzed_with_code_sha": res.get("analyzed_with_code_sha"),
        "verdicts": {
            k: v.get("verdict") for k, v in res["hypotheses"].items() if isinstance(v, dict)
        },
        "core_hypothesis": res["hypotheses"]["core_hypothesis"],
        "analysis_sha": hashlib.sha256(json.dumps(res, sort_keys=True).encode()).hexdigest()[:16],
    }
    with path.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
