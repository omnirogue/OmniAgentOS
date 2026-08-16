"""Kill -9 drill, one real OS process per tick (P1/P6 pattern).

Nothing here is mocked out of the failure path: the child dies with
``os._exit(9)`` mid-node, the parent starts a NEW interpreter, and the only
claim being tested is the one that matters — the irreversible external effect
appears in the ledger exactly once.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from omniagentos_loops.contracts import LoopStatus
from omniagentos_loops.templates import TEMPLATES

from omniagentos.contracts import ApprovalState

DRILL = Path(__file__).resolve().parent / "drills" / "kill_drill.py"
LOOPS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = LOOPS_DIR.parent


def _run(db: str, ledger: Path, *, crash_at: str = "", instance: str = "drill_loop"):
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{REPO_ROOT}:{LOOPS_DIR}"
    argv = [
        sys.executable,
        str(DRILL),
        "--db",
        db,
        "--ledger",
        str(ledger),
        "--instance",
        instance,
        "--template",
        "draft_approve_send",
    ]
    if crash_at:
        argv += ["--crash-at", crash_at]
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=180)


def _effects(ledger: Path) -> list[str]:
    return ledger.read_text(encoding="utf-8").splitlines() if ledger.exists() else []


def _approve(store, approval_id: str) -> None:
    assert store.decide_approval(approval_id, ApprovalState.APPROVED.value, "owner", "ok")


@pytest.fixture
def drill_env(db_path, loops_root, tmp_path):
    return db_path, tmp_path / "external_ledger.log"


def test_crash_between_effect_and_receipt_completion_never_duplicates(drill_env, store):
    """Crash AFTER the send, BEFORE the receipt completes: state unknown."""
    db, ledger = drill_env

    first = _run(db, ledger)
    assert first.returncode == 0, first.stderr
    parked = json.loads(first.stdout)
    assert parked["status"] == LoopStatus.PARKED.value
    _approve(store, parked["approval_id"])

    crashed = _run(db, ledger, crash_at="effect")
    assert crashed.returncode == -9 or crashed.returncode == 9, crashed
    assert len(_effects(ledger)) == 1

    resumed = _run(db, ledger)
    assert resumed.returncode == 0, resumed.stderr
    report = json.loads(resumed.stdout)
    assert len(_effects(ledger)) == 1, "the resume must NOT re-send"
    assert report["status"] == LoopStatus.ABORTED.value
    assert "EffectStateUnknown" in report["detail"]


def test_crash_after_receipt_completion_replays_and_finishes(drill_env, store):
    """The P7 window: crash after the receipt, before the superstep commits."""
    db, ledger = drill_env

    parked = json.loads(_run(db, ledger).stdout)
    _approve(store, parked["approval_id"])

    crashed = _run(db, ledger, crash_at="post-receipt")
    assert crashed.returncode in (9, -9), crashed
    assert len(_effects(ledger)) == 1

    resumed = _run(db, ledger)
    report = json.loads(resumed.stdout)
    assert len(_effects(ledger)) == 1, "the replayed node must reuse the receipt"
    assert report["status"] == LoopStatus.COMPLETED.value
    assert report["resumed"] is True


def test_a_clean_run_across_three_processes_sends_exactly_once(drill_env, store):
    db, ledger = drill_env
    parked = json.loads(_run(db, ledger).stdout)
    _approve(store, parked["approval_id"])
    done = json.loads(_run(db, ledger).stdout)
    again = json.loads(_run(db, ledger).stdout)

    assert done["status"] == LoopStatus.COMPLETED.value
    assert again["status"] == LoopStatus.COMPLETED.value
    assert len(_effects(ledger)) == 1, "a re-tick must replay the receipt, not re-send"


@pytest.mark.parametrize("template_name", sorted(TEMPLATES))
def test_every_template_survives_a_kill_at_its_first_effect(
    template_name, db_path, loops_root, tmp_path, store
):
    """Per-template kill drill via the shared harness.

    Each template's first effect node is driven to the same crash window; the
    guarantee under test is identical for all five, because they all reach the
    world through one seam.
    """
    from drills import template_kits

    kit = template_kits.KITS[template_name]
    ledger = tmp_path / f"{template_name}.log"
    result = template_kits.run_drill(
        template_name, kit, db_path=db_path, ledger=ledger, store=store
    )
    assert result["effect_lines"] == 1, result
    assert result["final_status"] in (
        LoopStatus.COMPLETED.value,
        LoopStatus.ABORTED.value,
        LoopStatus.IDLE.value,
    ), result
