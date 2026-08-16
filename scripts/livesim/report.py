#!/usr/bin/env python3
"""Generate the LiveSim coverage report from a run's ledger records.

    scripts/livesim/report.py [RUN_ID]     # default: latest run

Produces docs/testing/LIVESIM-COVERAGE-REPORT.md with: tests created/executed,
pass/fail/flaky/skip/xfail, per-category grid, per-type grid, cost + runtime,
evidence locations, and the model/provider/cost breakdown. Product and
test-infra issues come from docs/testing/LIVESIM-ISSUES.yaml.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import livesim_common as lc  # noqa: E402

OUT = lc.REPO / "docs" / "testing" / "LIVESIM-COVERAGE-REPORT.md"


def _latest_run() -> str | None:
    runs = sorted((lc.var_dir() / "runs").glob("livesim-*"))
    return runs[-1].name if runs else None


def _records(run_id: str) -> list[dict]:
    return [
        json.loads(p.read_text())
        for p in sorted(lc.run_dir(run_id).glob("*.json"))
        if p.name != "junit.xml"
    ]


def _flaky(run_id: str, recs: list[dict]) -> set[str]:
    """A test is flaky if a rerun of this run flipped its verdict.

    We detect reruns by run dirs named <run_id>-rerun-*: a nodeid that failed in
    the base run and passed in a rerun (or vice-versa) is flaky.
    """
    base = {r["nodeid"]: r["status"] for r in recs}
    flaky: set[str] = set()
    for rerun in sorted((lc.var_dir() / "runs").glob(f"{run_id}-rerun-*")):
        for p in rerun.glob("*.json"):
            if p.name == "junit.xml":
                continue
            r = json.loads(p.read_text())
            nid, st = r["nodeid"], r["status"]
            if nid in base and base[nid] != st:
                flaky.add(nid)
    return flaky


def build(run_id: str) -> str:
    recs = _records(run_id)
    if not recs:
        return f"# LiveSim coverage report\n\nNo records for run `{run_id}`.\n"
    flaky = _flaky(run_id, recs)

    by_status: dict[str, int] = defaultdict(int)
    by_cat: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_type: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    cost = 0.0
    runtime = 0.0
    models: dict[str, int] = defaultdict(int)
    llm_tests = 0
    for r in recs:
        st = r.get("status", "?")
        cat = r["nodeid"].split("::")[0].split("/")[-1].replace("test_", "").replace(".py", "")
        by_status[st] += 1
        by_cat[cat][st] += 1
        for t in r.get("types") or ["unclassified"]:
            by_type[t][st] += 1
        cost += float(r.get("cost_usd") or 0.0)
        runtime += float(r.get("duration_s") or 0.0)
        if r.get("model"):
            models[f"{r.get('provider')}/{r.get('model')}"] += 1
            llm_tests += 1

    ctx = recs[0]
    total = len(recs)
    passed = by_status.get("pass", 0)

    lines = []
    lines.append("# LiveSim coverage report")
    lines.append("")
    lines.append(f"- **Run**: `{run_id}`  ·  commit `{ctx.get('git_sha','')[:12]}` "
                 f"(dirty={ctx.get('git_dirty')})  ·  env `{ctx.get('env_label')}`  ·  "
                 f"host `{ctx.get('host')}`  ·  py {ctx.get('python')}")
    lines.append(f"- **Tests executed**: {total}  ·  "
                 f"pass {passed} · fail {by_status.get('fail',0)} · "
                 f"skip {by_status.get('skip',0)} · xfail {by_status.get('xfail',0)} · "
                 f"flaky {len(flaky)}")
    lines.append(f"- **Total test runtime**: {runtime:.1f}s (in-test)  ·  "
                 f"**LLM cost**: ${cost:.6f} across {llm_tests} LLM-touching tests")
    lines.append(f"- **Ledger**: `var/livesim/ledger.jsonl`  ·  "
                 f"**Records**: `var/livesim/runs/{run_id}/`  ·  "
                 f"**Evidence**: `var/livesim/evidence/{run_id}/`")
    lines.append("")

    # per-category grid
    lines.append("## Results by category")
    lines.append("")
    lines.append("| Category | tests | pass | fail | skip | xfail |")
    lines.append("|---|---|---|---|---|---|")
    for cat in sorted(by_cat):
        d = by_cat[cat]
        n = sum(d.values())
        lines.append(f"| {cat} | {n} | {d.get('pass',0)} | {d.get('fail',0)} | "
                     f"{d.get('skip',0)} | {d.get('xfail',0)} |")
    lines.append("")

    # per-type grid
    lines.append("## Results by test type")
    lines.append("")
    lines.append("| Type | tests | pass | fail | skip |")
    lines.append("|---|---|---|---|---|")
    for t in sorted(by_type):
        d = by_type[t]
        n = sum(d.values())
        lines.append(f"| {t} | {n} | {d.get('pass',0)} | {d.get('fail',0)} | {d.get('skip',0)} |")
    lines.append("")

    # model/cost breakdown
    lines.append("## Model / provider / cost")
    lines.append("")
    if models:
        for m, n in sorted(models.items(), key=lambda x: -x[1]):
            lines.append(f"- `{m}` — {n} test(s)")
    else:
        lines.append("- No metered-LLM calls this run (all tests were $0 API/DB/proc/fs probes, "
                     "or LLM probes skipped because the cheap endpoints were down).")
    lines.append("")

    # failures / flaky detail
    fails = [r for r in recs if r.get("status") == "fail"]
    if fails:
        lines.append("## Failing tests")
        lines.append("")
        for r in fails:
            lines.append(f"- `{r['nodeid']}` — {lc.preview(r.get('message',''), 160)}")
        lines.append("")
    if flaky:
        lines.append("## Flaky tests (verdict flipped on rerun)")
        lines.append("")
        for nid in sorted(flaky):
            lines.append(f"- `{nid}`")
        lines.append("")

    # skips detail
    skips = [r for r in recs if r.get("status") == "skip"]
    if skips:
        lines.append("## Skipped (blocked) tests")
        lines.append("")
        for r in skips:
            lines.append(f"- `{r['nodeid']}` — {lc.preview((r.get('notes') or [''])[0] or r.get('message',''), 140)}")
        lines.append("")

    lines.append("## Issues")
    lines.append("")
    lines.append("Structured product & test-infra issues: `docs/testing/LIVESIM-ISSUES.yaml`.")
    lines.append("")
    lines.append("---")
    lines.append(f"_Generated by scripts/livesim/report.py from run `{run_id}`._")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    run_id = (argv or sys.argv[1:] or [None])[0] or _latest_run()
    if run_id is None:
        print("no runs found")
        return 1
    text = build(run_id)
    OUT.write_text(text, encoding="utf-8")
    print(f"[livesim] wrote {OUT} for run {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
