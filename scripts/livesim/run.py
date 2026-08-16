#!/usr/bin/env python3
"""LiveSim runner — the single entry point future agents use to run the suite.

    scripts/livesim/run.py                       # run all categories once
    scripts/livesim/run.py --category reaper     # one category
    scripts/livesim/run.py --list                # list categories + test counts
    scripts/livesim/run.py --rerun-failures RUN  # re-run only the failures of a run
    scripts/livesim/run.py --summary [RUN]       # print the coverage summary
    scripts/livesim/run.py --issues              # print the structured issue log

It sets a deterministic run_id, exports the LiveSim env, invokes pytest against
tests/livesim with the marker override that RE-INCLUDES the livesim tests (the
default addopts excludes them), captures a JUnit XML, and then reconciles the
per-test telemetry sidecars the conftest wrote into a run summary.

Nothing here gates anything. Exit code reflects pytest's own result only so a
human can see red, but callers are expected to read the ledger, not the code.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import livesim_common as lc  # noqa: E402

REPO = lc.REPO
LIVESIM_DIR = REPO / "tests" / "livesim"
VENV_PY = REPO / ".venv" / "bin" / "python"


def _python() -> str:
    return str(VENV_PY) if VENV_PY.exists() else sys.executable


def _new_run_id() -> str:
    return f"livesim-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"


def categories() -> list[str]:
    cat_dir = LIVESIM_DIR / "categories"
    if not cat_dir.exists():
        return []
    return sorted(p.stem for p in cat_dir.glob("test_*.py"))


def cmd_list() -> int:
    py = _python()
    for cat in categories():
        path = LIVESIM_DIR / "categories" / f"{cat}.py"
        out = subprocess.run(
            [py, "-m", "pytest", str(path), "-o", "addopts=", "-m", "livesim", "--collect-only", "-q"],
            capture_output=True,
            text=True,
            cwd=str(REPO),
        )
        n = len([ln for ln in out.stdout.splitlines() if "::" in ln])
        print(f"  {cat:28s} {n:3d} tests")
    return 0


def _env(run_id: str) -> dict[str, str]:
    env = dict(os.environ)
    env["LIVESIM_RUN_ID"] = run_id
    env.setdefault("LIVESIM_ENV_LABEL", "live-local")
    env.setdefault("LIVESIM_API_BASE", lc.DEFAULT_API_BASE)
    # Never let an ambient DB/PYTHONPATH from a peer worktree poison collection.
    env.pop("PYTHONPATH", None)
    return env


def run_pytest(
    run_id: str,
    *,
    category: str | None,
    extra: list[str] | None = None,
) -> int:
    py = _python()
    junit = lc.run_dir(run_id) / "junit.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    target = str(LIVESIM_DIR / "categories" / f"test_{category}.py") if category else str(LIVESIM_DIR)
    argv = [
        py,
        "-m",
        "pytest",
        target,
        "-o",
        "addopts=",  # drop the repo default that excludes livesim
        "-m",
        "livesim",
        "-p",
        "no:randomly",
        "--timeout=120",
        "-v",
        f"--junitxml={junit}",
    ]
    if extra:
        argv.extend(extra)
    print(f"[livesim] run_id={run_id}\n[livesim] $ {' '.join(argv[2:])}\n", flush=True)
    proc = subprocess.run(argv, cwd=str(REPO), env=_env(run_id))
    return proc.returncode


def summarize(run_id: str) -> dict:
    recs = [
        json.loads(p.read_text())
        for p in sorted(lc.run_dir(run_id).glob("*.json"))
        if p.name != "junit.xml"
    ]
    by_status: dict[str, int] = {}
    cost = 0.0
    by_cat: dict[str, dict[str, int]] = {}
    for r in recs:
        s = r.get("status", "?")
        by_status[s] = by_status.get(s, 0) + 1
        cost += float(r.get("cost_usd") or 0.0)
        cat = (r.get("nodeid", "").split("::")[0].split("/")[-1]).replace("test_", "").replace(".py", "")
        by_cat.setdefault(cat, {})
        by_cat[cat][s] = by_cat[cat].get(s, 0) + 1
    return {
        "run_id": run_id,
        "total": len(recs),
        "by_status": by_status,
        "by_category": by_cat,
        "cost_usd": round(cost, 6),
    }


def cmd_summary(run_id: str | None) -> int:
    if run_id is None:
        runs = sorted((lc.var_dir() / "runs").glob("livesim-*"))
        if not runs:
            print("no runs found")
            return 1
        run_id = runs[-1].name
    s = summarize(run_id)
    print(json.dumps(s, indent=2))
    return 0


def cmd_issues() -> int:
    """Print the structured issue log grouped by severity."""
    path = REPO / "docs" / "testing" / "LIVESIM-ISSUES.yaml"
    if not path.exists():
        print(f"no issue log at {path}")
        return 1
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(path.read_text())
        issues = data.get("issues", [])
    except Exception:
        print(path.read_text())
        return 0
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "info": 4}
    for it in sorted(issues, key=lambda i: order.get(str(i.get("severity")), 9)):
        print(f"[{it.get('severity','?'):>4}] {it.get('id')} ({it.get('kind')}) — {it.get('title')}")
    print(f"\n{len(issues)} issues — full evidence/repro in {path}")
    return 0


def cmd_rerun_failures(run_id: str) -> int:
    recs = [json.loads(p.read_text()) for p in lc.run_dir(run_id).glob("*.json")]
    failed = [r["nodeid"] for r in recs if r.get("status") in ("fail", "error")]
    if not failed:
        print(f"[livesim] no failures in {run_id}")
        return 0
    new_id = f"{run_id}-rerun-{time.strftime('%H%M%S', time.gmtime())}"
    py = _python()
    junit = lc.run_dir(new_id) / "junit.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    argv = [py, "-m", "pytest", "-o", "addopts=", "-m", "livesim", "--timeout=120", "-v",
            f"--junitxml={junit}", *failed]
    print(f"[livesim] rerunning {len(failed)} failures as {new_id}")
    return subprocess.run(argv, cwd=str(REPO), env=_env(new_id)).returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LiveSim runner")
    ap.add_argument("--category", help="run one category (e.g. reaper, memory, api)")
    ap.add_argument("--list", action="store_true", help="list categories + test counts")
    ap.add_argument("--summary", nargs="?", const="__latest__", help="print run summary")
    ap.add_argument("--issues", action="store_true", help="print the structured issue log")
    ap.add_argument("--rerun-failures", metavar="RUN_ID", help="rerun failures of a prior run")
    ap.add_argument("--run-id", help="explicit run id (default: timestamp)")
    ap.add_argument("pytest_extra", nargs="*", help="extra args passed through to pytest")
    args = ap.parse_args(argv)

    if args.list:
        return cmd_list()
    if args.summary is not None:
        return cmd_summary(None if args.summary == "__latest__" else args.summary)
    if args.issues:
        return cmd_issues()
    if args.rerun_failures:
        return cmd_rerun_failures(args.rerun_failures)

    run_id = args.run_id or _new_run_id()
    rc = run_pytest(run_id, category=args.category, extra=args.pytest_extra or None)
    s = summarize(run_id)
    print("\n[livesim] summary:", json.dumps(s["by_status"]), f"cost=${s['cost_usd']}")
    print(f"[livesim] ledger:  {lc.ledger_path()}")
    print(f"[livesim] records: {lc.run_dir(run_id)}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
