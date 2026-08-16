#!/usr/bin/env python3
"""L5 — ETAR baseline (MASTER-PLAN / CALIBRATION-L0-L1-RESULTS).

After L0–L4 pass: measure formation-routed vs Opus-solo **prior ETAR** on 9 tasks
spanning ≥3 formations. Arms run **in parallel** per task; tasks fan out in a
thread pool.

This seeds ``formation_selections`` (Phase B6). It is a *prior-based* baseline
from modelintel speed/quality scores — not live wall-clock A/B. Production
outcomes should overwrite ``outcome`` / ``wall_clock_s`` later.

Never touches ~/OmniAgentOS product source.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPORT_DIR = ROOT / "var" / "calibration"
HANDOFF_REPORT = ROOT / "HANDOFF" / "CALIBRATION-L5-ETAR-RESULTS.md"
DB_PATH = REPORT_DIR / "l5_etar.db"

# 9 tasks × ≥3 formations (coding, creative, research, marketing, operations)
L5_TASKS: list[dict[str, str]] = [
    {
        "id": "t01",
        "formation": "coding",
        "difficulty": "medium",
        "goal": "Fix the login bug in the auth adapter and add a unit test",
    },
    {
        "id": "t02",
        "formation": "coding",
        "difficulty": "high",
        "goal": "Refactor the swarm scheduler ownership gate for recoverable actions",
    },
    {
        "id": "t03",
        "formation": "coding",
        "difficulty": "low",
        "goal": "Add a health-check endpoint and a unit test for it",
    },
    {
        "id": "t04",
        "formation": "research",
        "difficulty": "medium",
        "goal": "Research competitor pricing and summarize evidence with sources",
    },
    {
        "id": "t05",
        "formation": "research",
        "difficulty": "high",
        "goal": "Investigate competitive evidence for Initech enterprise positioning",
    },
    {
        "id": "t06",
        "formation": "creative",
        "difficulty": "medium",
        "goal": "Design a landing page hero image and ad concepts for a brand launch",
    },
    {
        "id": "t07",
        "formation": "marketing",
        "difficulty": "medium",
        "goal": "Launch an FB ad campaign funnel offer for the webinar",
    },
    {
        "id": "t08",
        "formation": "operations",
        "difficulty": "medium",
        "goal": "Automate weekly invoice reconciliation and support workflow",
    },
    {
        "id": "t09",
        "formation": "operations",
        "difficulty": "low",
        "goal": "Automate onboarding support workflow for Initech",
    },
]


@dataclass
class ArmResult:
    arm: str
    task_id: str
    formation_id: str | None
    confidence: float | None
    low_confidence: bool
    topology: str | None
    implementers: list[str]
    reviewer: str | None
    planner: str | None
    mechanical_gate: bool
    etar_s: float
    components: dict[str, Any]
    selection_id: str | None = None
    error: str | None = None


@dataclass
class TaskResult:
    task_id: str
    goal: str
    expected_formation: str
    difficulty: str
    bound_formation: str | None
    formation_match: bool
    arm_a: ArmResult | None = None
    arm_b: ArmResult | None = None
    comparison: dict[str, Any] = field(default_factory=dict)
    wall_s: float = 0.0


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_task(task: dict[str, str], conn_factory) -> TaskResult:
    """Run both arms for one task (sequential within task; tasks parallelized)."""
    from omniagentos.formation.etar import compare_arms, compute_etar
    from omniagentos.formation.selector import select_formation_with_confidence
    from omniagentos.formation.telemetry import record_selection
    from omniagentos.swarm.planner import build_plan

    t0 = time.monotonic()
    goal = task["goal"]
    tid = task["id"]
    difficulty = task["difficulty"]
    expected = task["formation"]

    raw = [
        {
            "id": "main",
            "title": "Main",
            "description": goal,
            "depends_on": [],
            "owned_paths": ["outputs/deliverables/out.txt"],
            "est_agent_minutes": 15,
            "est_manual_minutes": 45,
            "acceptance": "done",
            "verify_command": "true",
        }
    ]
    plan = build_plan(goal, raw)
    form = plan.formation
    bound = form.id if form else None
    match = bound == expected
    # also allow select_formation_with_confidence as cross-check
    try:
        sel = select_formation_with_confidence(goal)
        if bound is None and sel is not None:
            bound = getattr(sel, "formation_id", None) or getattr(sel, "id", None)
    except Exception:
        pass

    impl = list(form.implementers) if form and form.implementers else ["grok"]
    reviewer = form.reviewer if form else "opus"
    planner = form.planner if form else "opus"
    mech = bool(form.mechanical_gate) if form else False
    conf = float(form.confidence) if form else 0.0
    low = bool(form.low_confidence) if form else True
    topo = form.topology if form else None

    # Arm A — formation routed
    a_etar = compute_etar(
        arm="formation",
        formation_id=bound or expected,
        implementer=impl[0] if impl else "grok",
        reviewer=reviewer or "opus",
        mechanical_gate=mech,
        difficulty=difficulty,
    )
    # Arm B — Opus solo
    b_etar = compute_etar(
        arm="opus_solo",
        formation_id=bound or expected,
        implementer="opus",
        reviewer="opus",
        mechanical_gate=False,
        difficulty=difficulty,
    )
    comparison = compare_arms(a_etar, b_etar)

    conn = conn_factory()
    try:
        a_id = record_selection(
            conn,
            task_id=tid,
            goal=goal,
            arm="formation",
            formation_id=bound,
            confidence=conf,
            low_confidence=low,
            topology=topo,
            implementers=impl,
            reviewer=reviewer,
            planner=planner,
            mechanical_gate=mech,
            models={"implementer": impl[0] if impl else "grok", "reviewer": reviewer},
            predicted_etar_s=a_etar.etar_s,
            etar_components=a_etar.as_dict(),
            outcome="predicted",
            source="calibration",
            task_fingerprint=f"{expected}:{difficulty}",
        )
        b_id = record_selection(
            conn,
            task_id=tid,
            goal=goal,
            arm="opus_solo",
            formation_id=None,
            confidence=1.0,
            low_confidence=False,
            topology="solo",
            implementers=["opus"],
            reviewer="opus",
            planner="opus",
            mechanical_gate=False,
            models={"implementer": "opus", "reviewer": "opus"},
            predicted_etar_s=b_etar.etar_s,
            etar_components=b_etar.as_dict(),
            outcome="predicted",
            source="calibration",
            task_fingerprint=f"{expected}:{difficulty}",
        )
    finally:
        conn.close()

    arm_a = ArmResult(
        arm="formation",
        task_id=tid,
        formation_id=bound,
        confidence=conf,
        low_confidence=low,
        topology=topo,
        implementers=impl,
        reviewer=reviewer,
        planner=planner,
        mechanical_gate=mech,
        etar_s=a_etar.etar_s,
        components=a_etar.as_dict(),
        selection_id=a_id,
    )
    arm_b = ArmResult(
        arm="opus_solo",
        task_id=tid,
        formation_id=None,
        confidence=1.0,
        low_confidence=False,
        topology="solo",
        implementers=["opus"],
        reviewer="opus",
        planner="opus",
        mechanical_gate=False,
        etar_s=b_etar.etar_s,
        components=b_etar.as_dict(),
        selection_id=b_id,
    )
    return TaskResult(
        task_id=tid,
        goal=goal,
        expected_formation=expected,
        difficulty=difficulty,
        bound_formation=bound,
        formation_match=match,
        arm_a=arm_a,
        arm_b=arm_b,
        comparison=comparison,
        wall_s=time.monotonic() - t0,
    )


def _conn_factory(db_path: Path):
    import sqlite3

    def factory() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    return factory


def run_l5(*, workers: int = 6) -> dict[str, Any]:
    from omniagentos.db.migrate import migrate

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    migrate(str(DB_PATH))

    factory = _conn_factory(DB_PATH)
    results: list[TaskResult] = []
    errors: list[str] = []

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_run_task, task, factory): task["id"] for task in L5_TASKS}
        for fut in as_completed(futs):
            tid = futs[fut]
            try:
                results.append(fut.result())
                print(f"  done {tid}")
            except Exception as exc:
                err = f"{tid}: {exc}\n{traceback.format_exc()[-800:]}"
                errors.append(err)
                print(f"  FAIL {tid}: {exc}")

    results.sort(key=lambda r: r.task_id)
    wall = time.monotonic() - t0

    form_wins = sum(1 for r in results if r.comparison.get("winner") == "formation")
    solo_wins = sum(1 for r in results if r.comparison.get("winner") == "opus_solo")
    ties = sum(1 for r in results if r.comparison.get("winner") == "tie")
    mean_a = sum(r.arm_a.etar_s for r in results if r.arm_a) / max(len(results), 1)
    mean_b = sum(r.arm_b.etar_s for r in results if r.arm_b) / max(len(results), 1)
    match_n = sum(1 for r in results if r.formation_match)
    formations_seen = sorted({r.bound_formation for r in results if r.bound_formation})

    status = "PASS"
    if errors:
        status = "FAIL"
    elif len(results) < 8:
        status = "FAIL"
    elif len(formations_seen) < 3:
        status = "FAIL"
    elif match_n < 7:  # allow soft miss on edge goals
        status = "FAIL"

    payload = {
        "generated_at": _now(),
        "status": status,
        "wall_clock_s": round(wall, 2),
        "workers": workers,
        "n_tasks": len(results),
        "formations_seen": formations_seen,
        "formation_match": match_n,
        "form_wins": form_wins,
        "solo_wins": solo_wins,
        "ties": ties,
        "mean_formation_etar_s": round(mean_a, 2),
        "mean_opus_solo_etar_s": round(mean_b, 2),
        "hypothesis": (
            "formation_faster"
            if mean_a < mean_b
            else ("opus_solo_faster" if mean_b < mean_a else "tie")
        ),
        "errors": errors,
        "tasks": [
            {
                "task_id": r.task_id,
                "goal": r.goal,
                "expected": r.expected_formation,
                "bound": r.bound_formation,
                "match": r.formation_match,
                "difficulty": r.difficulty,
                "formation_etar_s": r.arm_a.etar_s if r.arm_a else None,
                "opus_solo_etar_s": r.arm_b.etar_s if r.arm_b else None,
                "winner": r.comparison.get("winner"),
                "delta_s": r.comparison.get("delta_s"),
                "formation_faster_pct": r.comparison.get("formation_faster_pct"),
                "arm_a": asdict(r.arm_a) if r.arm_a else None,
                "arm_b": asdict(r.arm_b) if r.arm_b else None,
            }
            for r in results
        ],
        "db_path": str(DB_PATH),
        "note": (
            "Prior-based ETAR from modelintel speed/quality — not live model wall-clock. "
            "Seeds formation_selections for production evidence overwrite."
        ),
    }
    return payload


def write_report(payload: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "l5-etar-results.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    lines = [
        "# Calibration L5 — ETAR baseline results",
        "",
        f"Generated {payload['generated_at']}.",
        "",
        f"**Overall: {payload['status']}**",
        "",
        "L5 measures **Expected Time to Accepted Result** for two arms on the same tasks:",
        "",
        "- **Arm A — formation**: fast implementer → mechanical gate (when set) → reviewer",
        "- **Arm B — Opus solo**: plan + implement + self-review on Opus",
        "",
        "This run is a **prior-based baseline** (modelintel speed/quality → ETAR formula). "
        "It seeds `formation_selections` so production wall-clock can replace priors later. "
        "It is **not** a live multi-model A/B (that is L5-live, cost/time gated).",
        "",
        "## Parallelism",
        "",
        f"- Tasks: **{payload['n_tasks']}** across a thread pool of **{payload['workers']}** workers",
        f"- Wall clock for full matrix: **{payload['wall_clock_s']}s**",
        "- Each task records both arms into the telemetry DB",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Status | **{payload['status']}** |",
        f"| Tasks | {payload['n_tasks']} |",
        f"| Formations bound | {', '.join(payload['formations_seen'])} |",
        f"| Formation match | {payload['formation_match']}/{payload['n_tasks']} |",
        f"| Mean ETAR formation | **{payload['mean_formation_etar_s']}s** |",
        f"| Mean ETAR opus solo | **{payload['mean_opus_solo_etar_s']}s** |",
        f"| Formation wins | {payload['form_wins']} |",
        f"| Opus-solo wins | {payload['solo_wins']} |",
        f"| Ties | {payload['ties']} |",
        f"| Hypothesis (priors) | `{payload['hypothesis']}` |",
        "",
        "## Per-task",
        "",
        "| ID | Expected | Bound | Diff | Form ETAR | Solo ETAR | Winner | Δs | Faster% |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for t in payload["tasks"]:
        lines.append(
            f"| {t['task_id']} | {t['expected']} | {t['bound']} | {t['difficulty']} | "
            f"{t['formation_etar_s']} | {t['opus_solo_etar_s']} | {t['winner']} | "
            f"{t['delta_s']} | {t['formation_faster_pct']}% |"
        )
    lines.extend(
        [
            "",
            "## ETAR formula",
            "",
            "```",
            "ETAR = t_initial + P(repair)×t_repair + P(escalation)×t_escalation + t_review",
            "```",
            "",
            "Priors: `configs/modelintel.yaml` speed → latency; domain quality → P(repair). "
            "Mechanical gate reduces P(repair). Opus solo multiplies t_initial (plan+impl) "
            "and lowers P(repair).",
            "",
            "## Telemetry",
            "",
            f"- DB: `{payload['db_path']}`",
            "- Table: `formation_selections` (migration 065)",
            f"- Rows: {payload['n_tasks'] * 2} (two arms × tasks)",
            "- Source: `calibration`",
            "",
            "## Pass criteria",
            "",
            "- ≥8 tasks completed without error",
            "- ≥3 distinct formations bound",
            "- ≥7/9 formation keyword matches",
            "- Both arms recorded per task",
            "",
            "## Re-run",
            "",
            "```bash",
            "cd ~/OmniAgentOS",
            'OMNIAGENTOS_HOME="$PWD" .venv/bin/python scripts/calibrate-l5-etar.py',
            "```",
            "",
        ]
    )
    if payload.get("errors"):
        lines.append("## Errors")
        lines.append("")
        for e in payload["errors"]:
            lines.append("```")
            lines.append(e[:1500])
            lines.append("```")
            lines.append("")
    HANDOFF_REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n=== L5 {payload['status']} ===")
    print(f"Report: {HANDOFF_REPORT}")


def main() -> int:
    workers = int(os.environ.get("L5_WORKERS", "6"))
    print(f"L5 ETAR baseline — {len(L5_TASKS)} tasks, {workers} parallel workers")
    payload = run_l5(workers=workers)
    write_report(payload)
    print(
        f"mean form={payload['mean_formation_etar_s']}s "
        f"solo={payload['mean_opus_solo_etar_s']}s "
        f"hypothesis={payload['hypothesis']}"
    )
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
