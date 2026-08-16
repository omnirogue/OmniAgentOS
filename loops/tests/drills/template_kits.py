"""Per-template drill kits: the minimum tool set that drives one template to its
first effect, plus the parent-side orchestration of the kill drill.

Shared by the drill child (which builds the tools) and the test (which spawns
processes), so the two can never disagree about what a template is being asked
to do.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omniagentos_loops.contracts import RiskTier
from omniagentos_loops.tools import LoopTool, ToolRegistry

from omniagentos.contracts import ApprovalState

DRILL = Path(__file__).resolve().parent / "kill_drill.py"
LOOPS_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = LOOPS_DIR.parent

DRAFT = {"to": "customer@example.com", "subject": "Re: order", "body": "shipping today"}


def _fixed(value: Any) -> Callable[..., Any]:
    return lambda **kwargs: value


def _t0(name: str, value: Any) -> LoopTool:
    return LoopTool(name=name, tier=RiskTier.T0, idempotency_key=lambda a: name, call=_fixed(value))


@dataclass(frozen=True)
class Kit:
    """How to drive one template to its first effect."""

    template: str
    effect_tool: str
    effect_tier: RiskTier
    effect_key: Callable[[dict[str, Any]], str]
    support: Callable[[], list[LoopTool]]
    params: dict[str, Any] = field(default_factory=dict)

    def registry(self, effect: Callable[..., Any]) -> ToolRegistry:
        registry = ToolRegistry()
        for tool in self.support():
            registry.register(tool)
        registry.register(
            LoopTool(
                name=self.effect_tool,
                tier=self.effect_tier,
                idempotency_key=self.effect_key,
                call=effect,
                description=f"drill effect {self.effect_tool}",
            )
        )
        return registry


KITS: dict[str, Kit] = {
    "draft_approve_send": Kit(
        template="draft_approve_send",
        effect_tool="send",
        effect_tier=RiskTier.T2,
        effect_key=lambda a: str(a["draft"]["to"]),
        support=lambda: [_t0("draft", dict(DRAFT))],
    ),
    "poll_classify_act_verify": Kit(
        template="poll_classify_act_verify",
        effect_tool="act",
        effect_tier=RiskTier.T2,
        effect_key=lambda a: str(a["item"]["id"]),
        support=lambda: [
            _t0("poll", [{"id": "msg-1"}]),
            _t0("classify", {"action": "reply"}),
            _t0("verify", {"verified": True}),
        ],
    ),
    "monitor_diagnose_repair_verify": Kit(
        template="monitor_diagnose_repair_verify",
        effect_tool="repair",
        effect_tier=RiskTier.T1,
        effect_key=lambda a: str(a["remedy"]),
        support=lambda: [
            _t0("monitor", {"api": "down"}),
            _t0("diagnose", {"remedy": "restart_api", "incident": "i-1"}),
            _t0("verify", {"verified": True}),
            LoopTool(
                name="escalate",
                tier=RiskTier.T3,
                idempotency_key=lambda a: "escalate",
                call=_fixed({"paged": True}),
            ),
        ],
        params={"allowed_remedies": ["restart_api"]},
    ),
    "generate_evaluate_improve": Kit(
        template="generate_evaluate_improve",
        effect_tool="publish",
        effect_tier=RiskTier.T2,
        effect_key=lambda a: "post-1",
        support=lambda: [
            _t0("generate", {"text": "draft"}),
            _t0("evaluate", {"score": 0.95, "feedback": ""}),
        ],
        params={"brief": "write a post"},
    ),
    "dispatch_await_summarize": Kit(
        template="dispatch_await_summarize",
        effect_tool="dispatch",
        effect_tier=RiskTier.T2,
        effect_key=lambda a: "spec-1",
        support=lambda: [
            _t0("poll_card", {"done": True, "outcome": "merged"}),
            LoopTool(
                name="summarize",
                tier=RiskTier.T1,
                idempotency_key=lambda a: "summary",
                call=_fixed("summary"),
            ),
        ],
        params={"spec": {"title": "fix the thing"}},
    ),
}


def _child(
    db_path: str, ledger: Path, template: str, *, crash_at: str
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{REPO_ROOT}:{LOOPS_DIR}"
    argv = [
        sys.executable,
        str(DRILL),
        "--db",
        db_path,
        "--ledger",
        str(ledger),
        "--template",
        template,
        "--instance",
        f"drill_{template}"[:64],
    ]
    if crash_at:
        argv += ["--crash-at", crash_at]
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=180)


def run_drill(
    template: str,
    kit: Kit,
    *,
    db_path: str,
    ledger: Path,
    store: Any,
    crash_at: str = "post-receipt",
) -> dict[str, Any]:
    """park (if T2+) -> approve -> crash mid-effect -> resume, in 3-4 processes."""
    first = _child(db_path, ledger, template, crash_at=crash_at)
    crashed = first.returncode in (9, -9)

    if not crashed:
        assert first.returncode == 0, first.stderr
        report = json.loads(first.stdout)
        assert report["status"] == "parked", report
        assert store.decide_approval(report["approval_id"], ApprovalState.APPROVED.value, "owner", "")
        second = _child(db_path, ledger, template, crash_at=crash_at)
        assert second.returncode in (9, -9), second
        crashed = True

    lines_at_crash = len(ledger.read_text().splitlines()) if ledger.exists() else 0
    final = _child(db_path, ledger, template, crash_at="")
    assert final.returncode == 0, final.stderr
    final_report = json.loads(final.stdout)
    return {
        "crashed": crashed,
        "effect_lines_at_crash": lines_at_crash,
        "effect_lines": len(ledger.read_text().splitlines()) if ledger.exists() else 0,
        "final_status": final_report["status"],
        "final": final_report,
    }


__all__ = ["DRAFT", "KITS", "Kit", "run_drill"]
