#!/usr/bin/env python3
"""Assemble a LiveSim review packet and send it to Kimi (cash-safe OAuth model).

Kimi's metered Moonshot org is PAUSED (2026-08-05 billing incident). This script
pins the OAuth-subscription model `kimi-code/k3` (`kimi -m kimi-code/k3`), which
rides the managed subscription — no metered-org spend. It NEVER uses the config
default (`fireworks/kimi-k3`, which is metered).

    scripts/livesim/kimi_review.py --round 1 --out var/livesim/kimi/round-1.md

The packet gives Kimi the suite's inventory, architecture, registry counts, the
latest coverage report, the issue log, and the exact review questions
(inventory, architecture, coverage, redundancy, reproducibility, missing risks).
The response is saved verbatim; the human/orchestrator evaluates each point.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import livesim_common as lc  # noqa: E402

# OAuth subscription — cash-safe (managed:kimi-code). `kimi-code/kimi-for-coding` is
# the subscription's default coding model and far faster than `kimi-code/k3`, whose
# default_effort=max times out a 48KB review packet at 600s. NEVER fireworks/kimi-k3
# (metered — the 2026-08-05 billing-pause org).
KIMI_MODEL = "kimi-code/kimi-for-coding"
DOCS = lc.REPO / "docs" / "testing"
CFG = lc.REPO / "configs"


def _read(p: Path, cap: int = 20000) -> str:
    if not p.exists():
        return f"(missing: {p})"
    text = p.read_text(encoding="utf-8", errors="replace")
    return text if len(text) <= cap else text[:cap] + f"\n...[truncated {len(text)-cap} chars]"


def build_packet(round_no: int, prior_feedback: str = "") -> str:
    parts = [
        f"# LiveSim review — round {round_no}",
        "",
        "You are an adversarial reviewer of a LIVE-SIMULATION diagnostic test suite built for "
        "OmniAgentOS. The suite is OBSERVATIONAL (never gates a merge); it exercises the "
        "running system (live :8485 API, live runtime DB, process table, the reaper stack, "
        "filesystem sandbox) plus cheap-LLM probes, and records full telemetry per run.",
        "",
        "Review these dimensions and be specific and terse — cite files/tests. For each point give a "
        "concrete, actionable recommendation the author can accept or reject:",
        "  1. TEST INVENTORY — are the right features covered? what live feature is untested?",
        "  2. ARCHITECTURE — is the harness (telemetry, isolation, runner, registry) sound?",
        "  3. COVERAGE — gaps by test-type (positive/negative/boundary/concurrency/recovery/"
        "permission/security/degradation/e2e) and by subsystem.",
        "  4. REDUNDANCY — duplicate or low-value tests to cut.",
        "  5. REPRODUCIBILITY — is a future agent able to discover, rerun, and trust results? "
        "determinism/idempotency/isolation weaknesses.",
        "  6. MISSING RISKS — safety holes (could a test mutate live prod?), flakiness traps, "
        "cost/telemetry gaps.",
        "",
        "Output as a numbered list of findings; each finding: [dimension] SEVERITY(high/med/low) — "
        "problem — recommendation. End with 'NO FURTHER IMPROVEMENTS' on its own line ONLY if the "
        "suite is genuinely in good shape.",
        "",
    ]
    if prior_feedback.strip():
        parts += ["## What changed since your last round (author's response)", "", prior_feedback, ""]
    parts += [
        "## Contract & inventory", "", _read(DOCS / "LIVESIM.md", 9000),
        "", "## Feature inventory", "", _read(DOCS / "LIVESIM-INVENTORY.md", 7000),
        "", "## Test registry (generated)", "", _read(CFG / "livesim-registry.yaml", 16000),
        "", "## Latest coverage report", "", _read(DOCS / "LIVESIM-COVERAGE-REPORT.md", 6000),
        "", "## Issue log", "", _read(DOCS / "LIVESIM-ISSUES.yaml", 8000),
        "", "## Harness (conftest telemetry+isolation)", "",
        _read(lc.REPO / "tests" / "livesim" / "conftest.py", 9000),
    ]
    return "\n".join(parts)


def run_kimi(packet: str, timeout: int = 900) -> tuple[int, str]:
    proc = subprocess.run(
        ["kimi", "-m", KIMI_MODEL, "-p", packet, "--output-format", "text"],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(lc.REPO),
    )
    return proc.returncode, (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.returncode else "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--prior", help="path to a file with the author's response since last round")
    ap.add_argument("--out", required=True)
    ap.add_argument("--packet-only", action="store_true", help="write the packet, don't call kimi")
    args = ap.parse_args(argv)

    prior = Path(args.prior).read_text() if args.prior and Path(args.prior).exists() else ""
    packet = build_packet(args.round, prior)
    outp = Path(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    (outp.parent / f"packet-round-{args.round}.md").write_text(packet, encoding="utf-8")
    if args.packet_only:
        print(f"[kimi-review] packet written ({len(packet)} chars); not calling kimi")
        return 0
    rc, resp = run_kimi(packet)
    outp.write_text(resp, encoding="utf-8")
    print(f"[kimi-review] round {args.round} rc={rc} -> {outp} ({len(resp)} chars)")
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
