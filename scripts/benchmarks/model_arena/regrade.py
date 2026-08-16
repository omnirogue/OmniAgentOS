"""Re-score a finished run from its saved completions.

    python -m scripts.benchmarks.model_arena.regrade <run-id>

Every answer is stored verbatim in ``raw.jsonl``, so fixing a grader — or adding a
stricter one — costs nothing and re-spends no contestant tokens. This rewrites
``quality.json`` and the report from disk, preserving the original timings.

Tier A is re-run through the real pytest suites; Tier B through the Python graders.
Differences against the previous scores are printed so a grader change is never
silent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.benchmarks.model_arena.providers import Completion  # noqa: E402
from scripts.benchmarks.model_arena.run_arena import (  # noqa: E402
    RESULTS_ROOT,
    aggregate,
    grade_tier_a,
    load_tier_a,
    write_report,
)
from scripts.benchmarks.model_arena.tasks_b import TIER_B  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: regrade <run-id>", file=sys.stderr)
        return 2
    run_id = args[0]
    run_dir = RESULTS_ROOT / run_id
    raw_path = run_dir / "raw.jsonl"
    if not raw_path.exists():
        print(f"no raw.jsonl at {raw_path}", file=sys.stderr)
        return 1

    tier_a = {t.task_id: t for t in load_tier_a()}
    tier_b = {t.task_id: t for t in TIER_B}

    # Last write wins per provider+tier+task, so a targeted re-run
    # (`--only-tasks`) supersedes the original attempt instead of double-counting it.
    latest: dict[tuple[str, str, str], dict] = {}
    for line in raw_path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        latest[(rec["provider"], rec["tier"], rec["task_id"])] = rec
    superseded = sum(1 for _ in raw_path.read_text().splitlines() if _.strip()) - len(latest)

    rows: list[dict] = []
    changes: list[str] = []
    for rec in latest.values():
        output = rec.pop("output", "")
        old = rec.get("score", 0.0)

        if rec["tier"] == "A" and rec["task_id"] in tier_a:
            comp = Completion(
                provider=rec["provider"], model=rec["model"], ok=bool(output.strip()), text=output
            )
            score, note = grade_tier_a(tier_a[rec["task_id"]], comp)
        elif rec["tier"] == "B" and rec["task_id"] in tier_b:
            if output.strip():
                try:
                    score, note = tier_b[rec["task_id"]].grade(output)
                except Exception as exc:  # noqa: BLE001
                    score, note = 0.0, f"grader error: {type(exc).__name__}: {exc}"
            else:
                score, note = 0.0, "no output"
        else:
            rows.append(rec)
            continue

        if abs(score - old) > 1e-9:
            changes.append(
                f"  {rec['provider']:17} {rec['tier']}/{rec['task_id']:22} "
                f"{old:.2f} -> {score:.2f}  ({note[:70]})"
            )
        rec["score"], rec["note"] = score, note
        rows.append(rec)

    summary = aggregate(rows)
    (run_dir / "quality.json").write_text(json.dumps({"rows": rows, "summary": summary}, indent=2))

    stress_path = run_dir / "stress.json"
    stress = json.loads(stress_path.read_text()) if stress_path.exists() else None
    report = write_report(run_dir, run_id, stress, summary)

    print(f"re-graded {len(rows)} rows from {raw_path}")
    if superseded:
        print(f"  ({superseded} earlier attempt(s) superseded by a later re-run)")
    if changes:
        print(f"\n{len(changes)} score change(s):")
        print("\n".join(changes))
    else:
        print("no score changes")
    print(f"\nreport -> {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
