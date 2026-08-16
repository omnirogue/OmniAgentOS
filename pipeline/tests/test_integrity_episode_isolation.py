"""Integrity's per-run state must be scoped to CATEGORY, and every alert needs
a stable key that never carries a measurement.

Reproduced at base_sha de98ec411b0820833bd3618826cc42ce821b532c (proposal
sha256:e447e8e633d18b5e61695afd56073d015e090d4b5b91bf79cb5463398b7017b3):
one liveness run records 3 episodes and writes 3 ALERTS.md lines; one
intervening invariants run (which raises zero alerts of its own) empties the
shared episode file to 0 keys and overwrites the shared heartbeat with
category=invariants; the next liveness run re-alerts all three, giving 6
ALERTS.md lines for 3 conditions that never cleared.

Each test below runs TWO categories against ONE root — a single-category test
passes trivially against the broken code and proves nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1] / "bridge" / "integrity.py"


def _queue(root: Path) -> Path:
    """A structurally valid, empty loopqueue."""
    for sub in ("findings", "proposals", "candidates", "inquiries", "rejected",
                "parked", "claims", "receipts", "state", "logs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    # One valid event: a ZERO-BYTE ledger is treated as unreadable (and must
    # exit 2, pinned separately), so it is not a "clean queue".
    (root / "ledger.jsonl").write_text(json.dumps({
        "ts": "2026-08-08T00:00:00Z", "role": "planner", "event": "proposed",
        "id": "sha256:" + "a" * 64, "detail": {},
    }) + "\n")
    (root / "state" / "budget.json").write_text(json.dumps({
        "disk_free_gb_min": 20, "load_avg_1m_max": 16, "wip_cap": 4,
        "updated_at": "2026-08-08T00:00:00Z",
    }))
    return root


def _run(root: Path, category: str, apply: bool = False) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(BRIDGE), "--loops-root", str(root), "--category", category]
    if apply:
        cmd.append("--apply")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def _alerts_lines(root: Path) -> list[str]:
    p = root / "ALERTS.md"
    if not p.exists():
        return []
    return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_quiet_category_does_not_wipe_liveness_episodes(tmp_path: Path) -> None:
    """A quiet category (raises zero alerts of its own) must not clear another
    category's episode state.

    Must fail against current integrity.py at de98ec41, where emit() builds
    one root-scoped state/integrity-episodes.json and reaps every key THIS
    category's run did not raise. Measured red: liveness -> invariants ->
    liveness yields 6 ALERTS.md lines for 3 conditions that never cleared,
    and the episode file is {} after the invariants run.
    """
    root = _queue(tmp_path / "q")

    p1 = _run(root, "liveness", apply=True)
    assert p1.returncode in (0, 1), f"liveness run 1 unexpected rc: {p1.stdout}{p1.stderr}"
    after_first_liveness = _alerts_lines(root)
    assert len(after_first_liveness) == 3, (
        f"fixture no longer produces exactly 3 liveness alerts: {after_first_liveness}"
    )

    p2 = _run(root, "invariants", apply=True)
    assert p2.returncode in (0, 1), f"invariants run unexpected rc: {p2.stdout}{p2.stderr}"

    p3 = _run(root, "liveness", apply=True)
    assert p3.returncode in (0, 1), f"liveness run 2 unexpected rc: {p3.stdout}{p3.stderr}"
    after_second_liveness = _alerts_lines(root)

    assert len(after_second_liveness) == 3, (
        f"a quiet invariants run wiped liveness's episode memory: expected 3 "
        f"total ALERTS.md lines after (liveness, invariants, liveness), got "
        f"{len(after_second_liveness)}: {after_second_liveness}"
    )


def test_heartbeat_is_per_category(tmp_path: Path) -> None:
    """Each category's heartbeat must survive every other category's run.

    Must fail against current integrity.py at de98ec41, where one
    root-scoped state/integrity-heartbeat.json carries the category only as
    a field — the last writer wins and the other categories' heartbeats
    vanish.
    """
    root = _queue(tmp_path / "q")

    p1 = _run(root, "reachability", apply=True)
    assert p1.returncode in (0, 1, 2), f"reachability run unexpected rc: {p1.stdout}{p1.stderr}"

    p2 = _run(root, "liveness", apply=True)
    assert p2.returncode in (0, 1), f"liveness run unexpected rc: {p2.stdout}{p2.stderr}"

    reachability_hb = root / "state" / "integrity-heartbeat-reachability.json"
    liveness_hb = root / "state" / "integrity-heartbeat-liveness.json"

    assert reachability_hb.exists(), (
        "reachability's heartbeat did not survive the subsequent liveness run "
        f"(only found: {sorted(p.name for p in (root / 'state').glob('integrity-heartbeat*'))})"
    )
    assert liveness_hb.exists(), "liveness heartbeat missing after its own run"

    rb = json.loads(reachability_hb.read_text())
    lb = json.loads(liveness_hb.read_text())

    assert rb["category"] == "reachability", rb
    assert lb["category"] == "liveness", lb
    assert "checks_run" in rb and rb["checks_run"] > 0, rb
    assert "checks_run" in lb and lb["checks_run"] > 0, lb


def test_rising_counter_in_alert_text_alerts_once(tmp_path: Path) -> None:
    """A still-true condition whose alert text embeds a rising hour counter
    must alert once, not once per occurrence.

    Must fail against the a[:60] text-prefix episode key at :630/:637 — the
    text changes as the hour counter advances, so the prefix key changes too
    and dedupe can never hold for this family. Must ALSO stay red against a
    path-only fix (scoping the episode file per category does not make a
    text-prefix key stable).
    """
    root = _queue(tmp_path / "q")

    p1 = _run(root, "liveness", apply=True)
    assert p1.returncode in (0, 1), f"liveness run 1 unexpected rc: {p1.stdout}{p1.stderr}"
    first = _alerts_lines(root)
    assert any("planner loop silent" in ln for ln in first), (
        f"fixture did not produce a planner-silent alert: {first}"
    )

    # Push the single ledger event further into the past so the alert text's
    # embedded hour counter advances (e.g. "silent 96h" -> "silent 120h").
    events = [json.loads(ln) for ln in (root / "ledger.jsonl").read_text().splitlines() if ln.strip()]
    for e in events:
        if e.get("role") == "planner":
            e["ts"] = "2026-08-01T00:00:00Z"
    (root / "ledger.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")

    p2 = _run(root, "liveness", apply=True)
    assert p2.returncode in (0, 1), f"liveness run 2 unexpected rc: {p2.stdout}{p2.stderr}"
    second = _alerts_lines(root)

    planner_silent_lines = [ln for ln in second if "planner loop silent" in ln]
    assert len(planner_silent_lines) == 1, (
        f"a still-true condition whose alert text advanced its hour counter "
        f"alerted more than once: {planner_silent_lines} (all lines: {second})"
    )
