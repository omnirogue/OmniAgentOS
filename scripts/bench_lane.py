#!/usr/bin/env python3
"""Benchmark a test lane N times and emit a falsifiable JSON record.

Why this exists
---------------
Under varying machine load, the same lane on the same box can produce wildly
different wall clocks, cumulative times and failure counts. A single wall-clock
number is therefore not evidence of anything. This harness:

  * refuses to run above a load threshold (default 4.0 one-minute average),
  * repeats the lane N times,
  * reports CUMULATIVE test seconds and eff_par (cum/wall) alongside wall clock,
    because wall alone cannot distinguish "tests got faster" from "box got quieter",
  * counts tests at/over the pytest timeout separately from failures, because a
    timeout hang is not test work (22 such hangs were 66.6% of the cumulative time in
    var/test-reports/fast-lane-baseline.xml, which TESTING.md quoted as its baseline).

Usage
-----
    python3 scripts/bench_lane.py --lane test-fast --runs 5
    python3 scripts/bench_lane.py --lane test-fast --runs 3 --max-load 8 --tag phase1
    python3 scripts/bench_lane.py --cmd "uv run pytest -q -n 16 tests/scope" --runs 5

Exit codes: 0 ok, 2 refused (load too high), 3 no JUnit report, 4 refused (dirty tree),
            5 aborted (HEAD moved mid-benchmark).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REPORTS = REPO / "var" / "test-reports"
# pyproject.toml [tool.pytest.ini_options] timeout = 180
TIMEOUT_S = 180.0
TIMEOUT_EPS = 1.0


def load1() -> float:
    return os.getloadavg()[0]


def tree_fingerprint() -> tuple[str, bool]:
    """(HEAD sha, is_dirty). A benchmark whose input moves mid-run measures nothing.

    This is not hypothetical: a sibling session once ran
    `git checkout main` while a 3-run benchmark was in flight, so runs 1-2
    executed the branch's -n 16 lane and run 3 was queued against main's -n 8.
    The harness had no way to notice. It does now.
    """
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=REPO, capture_output=True, text=True,
        ).stdout.strip()
    )
    return sha, dirty


def wait_for_quiet(max_load: float, patience_s: int) -> float:
    """Block until the 1-min load drops below max_load. Returns the load we started at."""
    deadline = time.monotonic() + patience_s
    while True:
        cur = load1()
        if cur < max_load:
            return cur
        if time.monotonic() > deadline:
            print(
                f"REFUSED: 1-min load {cur:.2f} >= {max_load} after waiting {patience_s}s.\n"
                "A benchmark taken on a contended box is not evidence. Quiesce the agent\n"
                "fleet or raise --max-load deliberately and record that you did.",
                file=sys.stderr,
            )
            sys.exit(2)
        print(f"  load {cur:.2f} >= {max_load}, waiting...", file=sys.stderr)
        time.sleep(15)


def parse_junit(path: Path) -> dict:
    root = ET.parse(path).getroot()
    durations: list[tuple[float, str]] = []
    failed = errors = skipped = 0
    for tc in root.iter("testcase"):
        d = float(tc.get("time") or 0.0)
        name = f"{tc.get('classname', '')}::{tc.get('name', '')}"
        durations.append((d, name))
        if tc.find("failure") is not None:
            failed += 1
        if tc.find("error") is not None:
            errors += 1
        if tc.find("skipped") is not None:
            skipped += 1
    durations.sort(reverse=True)
    times = [d for d, _ in durations]
    cum = sum(times)
    timeouts = [(d, n) for d, n in durations if d >= TIMEOUT_S - TIMEOUT_EPS]
    return {
        "tests": len(durations),
        "cum_s": round(cum, 1),
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "timeout_hangs": len(timeouts),
        "timeout_hang_s": round(sum(d for d, _ in timeouts), 1),
        "longest_test_s": round(times[0], 1) if times else 0.0,
        "longest_test": durations[0][1] if durations else "",
        "top25": [{"s": round(d, 2), "test": n} for d, n in durations[:25]],
    }


def pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round((p / 100) * (len(s) - 1)))))
    return s[k]


def _emit(runs: list[dict], cmd: str, args, pinned_sha: str, dirty0: bool,
          partial: bool = False) -> dict:
    walls = [r["wall_s"] for r in runs]
    cums = [r["cum_s"] for r in runs]
    fails = [r["failed"] for r in runs]
    p50, p95 = pct(walls, 50), pct(walls, 95)
    out = {
        "cmd": cmd,
        "pinned_sha": pinned_sha,
        "dirty": dirty0,
        "runs": runs,
        "n_runs": len(runs),
        "max_load": args.max_load,
        "p50_wall_s": p50,
        "p95_wall_s": p95,
        "wall_spread": round((max(walls) - min(walls)) / min(walls), 3) if min(walls) else 0,
        "cum_s": round(statistics.median(cums), 1),
        "cum_spread": round((max(cums) - min(cums)) / min(cums), 3) if min(cums) else 0,
        "eff_par": round(statistics.median([r["eff_par"] for r in runs]), 2),
        "failed_min": min(fails),
        "failed_max": max(fails),
        "failed_stable": min(fails) == max(fails),
        "timeout_hangs_max": max(r["timeout_hangs"] for r in runs),
        "longest_test_s": max(r["longest_test_s"] for r in runs),
        "stable": (p95 - p50) / p50 < 0.10 if p50 else False,
        "top25": runs[-1]["top25"],
    }
    out["partial"] = partial
    dest = REPORTS / f"bench-{args.tag}.json"
    dest.write_text(json.dumps(out, indent=2) + "\n")

    print(f"\n{'='*64}\nBENCH {args.tag}  ({len(runs)} runs)\n{'='*64}")
    print(f"  p50 wall        {out['p50_wall_s']}s   p95 {out['p95_wall_s']}s")
    print(f"  cumulative      {out['cum_s']}s   (median)")
    print(f"  eff_par         {out['eff_par']}")
    print(f"  failures        {out['failed_min']}-{out['failed_max']}"
          f"  {'STABLE' if out['failed_stable'] else 'UNSTABLE'}")
    print(f"  timeout hangs   {out['timeout_hangs_max']}")
    print(f"  longest test    {out['longest_test_s']}s  <- critical-path floor")
    print(f"  wall spread     {out['wall_spread']*100:.1f}%   "
          f"{'OK' if out['stable'] else 'NOT STABLE (p95-p50 >= 10%)'}")
    if partial:
        print("  PARTIAL: tree moved mid-benchmark; fewer runs than requested.")
    print(f"  -> {dest}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane", default="test-fast", help="make target to run")
    ap.add_argument("--cmd", default=None, help="explicit command instead of a make target")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--max-load", type=float, default=4.0)
    ap.add_argument("--patience", type=int, default=600, help="seconds to wait for quiet")
    ap.add_argument("--tag", default="latest")
    ap.add_argument("--junit", default=str(REPORTS / "fast-lane-latest.xml"))
    ap.add_argument("--allow-dirty", action="store_true",
                    help="benchmark an uncommitted tree (records the fact in the output)")
    args = ap.parse_args()

    REPORTS.mkdir(parents=True, exist_ok=True)
    cmd = args.cmd or f"make {args.lane}"
    junit = Path(args.junit)

    runs = []
    pinned_sha, dirty0 = tree_fingerprint()
    if dirty0 and not args.allow_dirty:
        print(
            "REFUSED: working tree is dirty (tracked files modified).\n"
            "A benchmark against uncommitted state is not reproducible evidence.\n"
            "Commit/stash, or pass --allow-dirty and record that you did.",
            file=sys.stderr,
        )
        return 4
    print(
        f"benchmarking: {cmd}   runs={args.runs}  max_load={args.max_load}\n"
        f"pinned to {pinned_sha[:12]}{' (DIRTY, allowed)' if dirty0 else ''}",
        file=sys.stderr,
    )
    for i in range(args.runs):
        start_load = wait_for_quiet(args.max_load, args.patience)
        sha_now, dirty_now = tree_fingerprint()
        if sha_now != pinned_sha or (dirty_now and not args.allow_dirty):
            # The runs already completed are still valid — they ran against the pinned
            # input. Persist them (flagged partial) rather than throwing away good data;
            # only the aggregate "stable" claim is withheld.
            print(
                f"ABORTED before run {i+1}: the tree moved under the benchmark.\n"
                f"  pinned : {pinned_sha}\n  now    : {sha_now}{' +dirty' if dirty_now else ''}\n"
                f"Keeping the {len(runs)} run(s) that DID execute against the pinned tree; "
                f"marking the record partial.",
                file=sys.stderr,
            )
            if runs:
                _emit(runs, cmd, args, pinned_sha, dirty0, partial=True)
            return 5
        if junit.exists():
            junit.unlink()
        t0 = time.monotonic()
        proc = subprocess.run(cmd, shell=True, cwd=REPO, capture_output=True, text=True)
        wall = time.monotonic() - t0
        if not junit.exists():
            print(f"run {i+1}: no JUnit report at {junit}", file=sys.stderr)
            print(proc.stdout[-2000:], file=sys.stderr)
            return 3
        rec = parse_junit(junit)
        rec.update(
            run=i + 1,
            wall_s=round(wall, 1),
            start_load=round(start_load, 2),
            end_load=round(load1(), 2),
            exit_code=proc.returncode,
            eff_par=round(rec["cum_s"] / wall, 2) if wall else 0.0,
        )
        runs.append(rec)
        print(
            f"  run {i+1}/{args.runs}: wall={rec['wall_s']}s cum={rec['cum_s']}s "
            f"eff_par={rec['eff_par']} failed={rec['failed']} hangs={rec['timeout_hangs']} "
            f"load {rec['start_load']}->{rec['end_load']}",
            file=sys.stderr,
        )

    _emit(runs, cmd, args, pinned_sha, dirty0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
