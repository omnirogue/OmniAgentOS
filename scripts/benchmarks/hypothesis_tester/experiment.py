"""Hypothesis Tester CLI.

  python3 -m scripts.benchmarks.hypothesis_tester.experiment estimate [matrix flags]
  python3 -m scripts.benchmarks.hypothesis_tester.experiment run --exp-id X [matrix flags]
  python3 -m scripts.benchmarks.hypothesis_tester.experiment analyze --exp-id X

Matrix flags (defaults = the pre-registered DESIGN.md §4 matrix):
  --families gatekeeper,strategy,trapcli   --arms none,outcomes,...
  --seeds 0,1,2,3   --models nemo,llama8b,gptoss   --episodes 16
  --workers 8   --dry-run (offline scripted agent, no spend)

Runs are resumable: re-running the same --exp-id skips episodes already in
episodes.jsonl. exp ids prefixed `pilot-` are excluded from confirmatory
analysis by convention (DESIGN.md §4).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from .analyze import analyze, instrument_code_sha
from .client import MODELS, OpenRouterClient
from .memory import ARMS
from .runner import EpisodeLog, RunSpec, load_donor_records, run_one
from .worlds import FAMILIES

REPO_ROOT = Path(__file__).resolve().parents[3]


def var_dir() -> Path:
    base = os.environ.get("HYPOTHESIS_TESTER_VAR")
    return Path(base) if base else REPO_ROOT / "var" / "hypothesis_tester"


# Part of the frozen config identity: resuming an exp id after the prompts,
# worlds, verifiers, or analysis code changed would silently mix two different
# instruments under one 'frozen' experiment (HT-004). The hash itself lives in
# analyze.py so the analyze() guard and this writer share one definition.
_code_sha = instrument_code_sha


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def build_specs(args: argparse.Namespace) -> list[RunSpec]:
    specs = []
    donor = getattr(args, "donor_model", None)
    for family in args.families:
        for seed in args.seeds:
            for arm in args.arms:
                for alias in args.models:
                    if donor and alias == donor:
                        continue  # a model never 'transfers' from itself
                    specs.append(
                        RunSpec(
                            family=family,
                            seed=seed,
                            arm=arm,
                            model=MODELS[alias],
                            episodes=args.episodes,
                            donor_alias=donor,
                        )
                    )
    return specs


def freeze_config(exp_dir: Path, args: argparse.Namespace, n_specs: int) -> dict:
    config = {
        "exp_id": args.exp_id,
        "families": args.families,
        "arms": args.arms,
        "donor_exp": getattr(args, "donor_exp", None),
        "donor_model": getattr(args, "donor_model", None),
        "seeds": args.seeds,
        "models": {
            a: {
                "model_id": MODELS[a].model_id,
                "price_in": MODELS[a].price_in,
                "price_out": MODELS[a].price_out,
            }
            for a in args.models
        },
        "episodes": args.episodes,
        "runs": n_specs,
        "dry_run": args.dry_run,
        "code_sha": _code_sha(),
        "git_sha": _git_sha(),
        "created_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    identity = {k: v for k, v in config.items() if k not in ("git_sha", "created_at")}
    config["config_sha"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()[:16]
    path = exp_dir / "config.json"
    if path.exists():
        existing = json.loads(path.read_text())
        if existing.get("config_sha") != config["config_sha"]:
            sys.exit(
                f"refusing to resume {args.exp_id} with a different matrix "
                f"(existing {existing.get('config_sha')} != {config['config_sha']}); "
                "use a new --exp-id"
            )
        return existing
    path.write_text(json.dumps(config, indent=2, sort_keys=True))
    return config


def cmd_run(args: argparse.Namespace) -> None:
    exp_dir = var_dir() / "runs" / args.exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    specs = build_specs(args)
    freeze_config(exp_dir, args, len(specs))
    log = EpisodeLog(exp_dir / "episodes.jsonl")
    client = None if args.dry_run else OpenRouterClient()
    print(
        f"[{args.exp_id}] {len(specs)} runs x {args.episodes} episodes "
        f"({'dry-run' if args.dry_run else 'live'}), workers={args.workers}",
        flush=True,
    )
    donors: dict[str, list] = {}
    if getattr(args, "donor_exp", None):
        donor_log = var_dir() / "runs" / args.donor_exp / "episodes.jsonl"
        for s in specs:
            donor_run = f"{s.family}-s{s.seed}-{s.arm}-{args.donor_model}"
            if donor_run not in donors:
                donors[donor_run] = load_donor_records(donor_log, donor_run)
                if not donors[donor_run]:
                    sys.exit(f"donor run {donor_run} has no episodes in {args.donor_exp}")
    results = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                run_one, s, args.exp_id, log, client, args.dry_run,
                donors.get(f"{s.family}-s{s.seed}-{s.arm}-{getattr(args, 'donor_model', None)}"),
            ): s
            for s in specs
        }
        for i, fut in enumerate(cf.as_completed(futures), 1):
            spec = futures[fut]
            try:
                summary = fut.result()
            except Exception as e:  # noqa: BLE001 — a run must never kill the matrix
                summary = {"run_id": spec.run_id, "crashed": str(e)[:300]}
            results.append(summary)
            late = summary.get("late_success")
            print(
                f"[{i}/{len(specs)}] {spec.run_id}: "
                f"late={'-' if late is None else f'{late:.2f}'} "
                f"errors={summary.get('errors', '?')} cost=${summary.get('cost_usd', 0):.4f}",
                flush=True,
            )
    results.sort(key=lambda r: r.get("run_id", ""))
    with (exp_dir / "results.jsonl").open("w") as f:
        for r in results:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    total_cost = sum(r.get("cost_usd", 0) for r in results)
    crashed = [r["run_id"] for r in results if "crashed" in r]
    summary = {
        "exp_id": args.exp_id,
        "runs": len(results),
        "crashed": crashed,
        "total_cost_usd": round(total_cost, 4),
        "finished_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (exp_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(f"done: {json.dumps(summary)}", flush=True)


def cmd_analyze(args: argparse.Namespace) -> None:
    exp_dir = var_dir() / "runs" / args.exp_id
    # The stale-instrument guard lives inside analyze() itself, so every entry
    # point shares it (HT-004b). getattr: a hand-built Namespace without the
    # flag must get the evidence-bearing refusal, not AttributeError (HT-010).
    try:
        res = analyze(
            exp_dir,
            args.episodes,
            allow_stale_instrument=getattr(args, "allow_stale_instrument", False),
        )
    except RuntimeError as e:
        sys.exit(str(e))
    print((exp_dir / "ANALYSIS.md").read_text())
    print(f"\nanalysis.json + ANALYSIS.md written under {exp_dir}")
    core = res["hypotheses"]["core_hypothesis"]
    print(f"core hypothesis verdict: {core}")


def cmd_estimate(args: argparse.Namespace) -> None:
    specs = build_specs(args)
    eps = len(specs) * args.episodes
    # Rough averages measured in the pilot: ~1.3k prompt + 200 completion tokens.
    cost = sum(
        args.episodes * (1300 * s.model.price_in + 200 * s.model.price_out) / 1e6 for s in specs
    )
    print(f"runs={len(specs)} episodes={eps} est_cost=${cost:.2f}")


def _csv(kind: str, allowed: tuple[str, ...] | None = None):
    def parse(raw: str) -> list:
        items = [x.strip() for x in raw.split(",") if x.strip()]
        if allowed:
            bad = [x for x in items if x not in allowed]
            if bad:
                raise argparse.ArgumentTypeError(f"unknown {kind}: {bad} (allowed: {allowed})")
        return items

    return parse


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "analyze", "estimate"):
        sp = sub.add_parser(name)
        if name != "estimate":
            sp.add_argument("--exp-id", required=True)
        sp.add_argument("--families", type=_csv("family", FAMILIES), default=list(FAMILIES))
        sp.add_argument("--arms", type=_csv("arm", ARMS), default=list(ARMS))
        sp.add_argument(
            "--seeds",
            type=lambda s: [int(x) for x in s.split(",") if x.strip()],
            default=[0, 1, 2, 3],
        )
        sp.add_argument(
            "--models",
            type=_csv("model", tuple(MODELS)),
            # Default stays the ORIGINAL pinned trio; round-10 screening
            # candidates must be requested explicitly.
            default=["nemo", "llama8b", "gptoss"],
        )
        sp.add_argument("--episodes", type=int, default=16)
        sp.add_argument("--workers", type=int, default=8)
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--donor-exp", help="exp id whose episode history feeds memory (round 11)")
        sp.add_argument("--donor-model", choices=tuple(MODELS), help="model alias whose history is consumed")
        if name == "analyze":
            sp.add_argument("--allow-stale-instrument", action="store_true")
    args = p.parse_args(argv)
    {"run": cmd_run, "analyze": cmd_analyze, "estimate": cmd_estimate}[args.cmd](args)


if __name__ == "__main__":
    main()
