"""Tests for hostile conftest, workspace binding, and decision-point signature verification."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from omniagentos.contracts import RunState
from omniagentos.db.store import SqliteStore
from omniagentos.policy import load_policy
from omniagentos.scheduler.gate_evidence import (
    GateEvidenceStore,
    evidence_rejections,
    workspace_digest_for,
)
from omniagentos.scheduler.gate_runner import (
    GateRunRequest,
    PytestGateRunner,
    produce_gate_evidence,
)
from omniagentos.scheduler.routines_settle import evaluate_gate, settle_pending
from omniagentos.scheduler.routines_tick import tick
from omniagentos.scheduler.store import RoutinesStore
from tests.routines.conftest import valid_routine_payload
from tests.support.db_template import make_store

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

SUITE_4_TESTS = """
def test_1(): assert True
def test_2(): assert True
def test_3(): assert True
def test_4(): assert True
"""


def _git_workspace(tmp_path: Path, code: str = SUITE_4_TESTS) -> Path:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    (root / "suite").mkdir(parents=True, exist_ok=True)
    (root / "suite" / "test_4.py").write_text(code, encoding="utf-8")
    (root / ".gitignore").write_text(".pytest_cache\n__pycache__\nvar\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(root), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(root),
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(root), check=True, capture_output=True)
    return root


def test_clean_workspace_passes_and_mints_clean_evidence(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    store = GateEvidenceStore(tmp_path / "evidence")
    runner = PytestGateRunner(store)
    req = GateRunRequest(
        routine_id="rt-1",
        run_id="run-1",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": "pytest suite", "expected_exit_code": 0},
        workspace=workspace,
    )

    outcome = produce_gate_evidence(runner, store, req)
    assert outcome.status == "evidence"
    ev = outcome.evidence
    assert ev is not None
    assert ev.checks_collected == 4
    assert ev.checks_passed == 4
    assert ev.deselected_count == 0
    assert ev.workspace_tree_clean is True


def test_hostile_conftest_untracked_deselecting(tmp_path: Path) -> None:
    """P4 (a): untracked deselecting conftest produces NO evidence, via dirty tree.

    The status is ``unavailable`` rather than ``refused`` since
    ``GateWorkspaceUnusable`` split the workspace causes out of the refusal
    class. What P4 protects is unchanged and is asserted directly below: a
    hostile untracked conftest never yields evidence, so the deselection can
    never become a pass. The relabelling exists because ``routines_settle``
    turns a refusal into ``gate_passed=0`` against the auto-pause floor, and a
    dirty workspace judged nothing at all. Consumers that must not proceed on
    absence — ``integration/promote.py`` — require ``status == "evidence"`` and
    are unaffected by which non-evidence status this is.
    """
    workspace = _git_workspace(tmp_path)
    conftest = workspace / "suite" / "conftest.py"
    conftest.write_text(
        "def pytest_collection_modifyitems(items): items.clear()\n", encoding="utf-8"
    )

    store = GateEvidenceStore(tmp_path / "evidence")
    runner = PytestGateRunner(store)
    req = GateRunRequest(
        routine_id="rt-1",
        run_id="run-1",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": "pytest suite", "expected_exit_code": 0},
        workspace=workspace,
    )

    outcome = produce_gate_evidence(runner, store, req)
    assert outcome.status == "unavailable"
    assert "uncommitted changes or untracked files" in outcome.detail
    # The security property, asserted rather than implied by the label:
    assert outcome.evidence is None, "a hostile untracked conftest must yield no evidence"
    assert outcome.status != "evidence", "nothing here may be treated as a pass"
    assert store.load("rt-1", "run-1") is None, "and nothing may be recorded"


def test_hostile_conftest_committed_deselecting_3_of_4(tmp_path: Path) -> None:
    """P4 (b): committed conftest deselecting 3 via pytest_collection_modifyitems."""
    workspace = _git_workspace(tmp_path)
    conftest = workspace / "suite" / "conftest.py"
    conftest.write_text(
        "def pytest_collection_modifyitems(items):\n    del items[1:]\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=str(workspace), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "add conftest"], cwd=str(workspace), check=True, capture_output=True
    )

    store = GateEvidenceStore(tmp_path / "evidence")
    runner = PytestGateRunner(store)
    req = GateRunRequest(
        routine_id="rt-1",
        run_id="run-1",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": "pytest suite", "expected_exit_code": 0},
        workspace=workspace,
    )

    outcome = produce_gate_evidence(runner, store, req)
    assert outcome.status == "evidence"
    ev = outcome.evidence
    assert ev is not None
    assert ev.deselected_count > 0

    rejections = evidence_rejections(
        ev,
        routine_id="rt-1",
        run_id="run-1",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": "pytest suite", "expected_exit_code": 0},
        workspace_digest=workspace_digest_for(workspace),
        now=NOW,
        verifier=store.verify,
    )
    assert any("deselected" in r for r in rejections)


def test_hostile_conftest_untracked_collect_ignore(tmp_path: Path) -> None:
    """P4 (c): untracked conftest with collect_ignore produces NO evidence, via dirty tree.

    See ``test_hostile_conftest_untracked_deselecting`` for why a dirty
    workspace is ``unavailable`` rather than ``refused``; the protection that
    P4 names is asserted here directly.
    """
    workspace = _git_workspace(tmp_path)
    conftest = workspace / "suite" / "conftest.py"
    conftest.write_text("collect_ignore = ['test_4.py']\n", encoding="utf-8")

    store = GateEvidenceStore(tmp_path / "evidence")
    runner = PytestGateRunner(store)
    req = GateRunRequest(
        routine_id="rt-1",
        run_id="run-1",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": "pytest suite", "expected_exit_code": 0},
        workspace=workspace,
    )

    outcome = produce_gate_evidence(runner, store, req)
    assert outcome.status == "unavailable"
    assert outcome.evidence is None
    assert store.load("rt-1", "run-1") is None


def test_decision_point_forgery_rejected(tmp_path: Path) -> None:
    """P5 / B6: in-memory GateEvidence with empty or foreign-key signature is rejected."""
    workspace = _git_workspace(tmp_path)
    store1 = GateEvidenceStore(tmp_path / "store1")
    store2 = GateEvidenceStore(tmp_path / "store2")

    runner = PytestGateRunner(store1)
    req = GateRunRequest(
        routine_id="rt-1",
        run_id="run-1",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": "pytest suite", "expected_exit_code": 0},
        workspace=workspace,
    )

    outcome = produce_gate_evidence(runner, store1, req)
    ev = outcome.evidence
    assert ev is not None

    res = evaluate_gate(
        "test_command",
        req.gate_config,
        {"id": "run-1", "state": RunState.COMPLETED.value},
        evidence=ev,
        routine_id="rt-1",
        iteration=1,
        workspace_digest=workspace_digest_for(workspace),
        now=NOW,
        evidence_store=store2,
    )
    assert res is False

    res_no_store = evaluate_gate(
        "test_command",
        req.gate_config,
        {"id": "run-1", "state": RunState.COMPLETED.value},
        evidence=ev,
        routine_id="rt-1",
        iteration=1,
        workspace_digest=workspace_digest_for(workspace),
        now=NOW,
        evidence_store=None,
    )
    assert res_no_store is False


def test_metric_threshold_settles_as_gate_unverifiable(tmp_path: Path) -> None:
    """P6 / §7: metric_threshold gates (with no trusted evidence path) settle with gate_passed=None, excluded from acceptance floor."""
    db = make_store(SqliteStore, tmp_path / "test.db")
    routines = RoutinesStore(db)

    payload = valid_routine_payload(
        name="M1",
        trigger_config={"cron": "* * * * *"},
        task_template={"title": "do it", "harness": "cli-grok"},
        gate_type="metric_threshold",
        gate_config={"metric": "score", "operator": ">=", "threshold": 1.0},
    )
    routine = routines.create_routine(payload)
    _fired = tick(db, load_policy(), now=NOW)["fired"][0]
    routine_run = routines.list_runs(routine["id"])[0]
    db.update_run(
        routine_run["run_id"],
        {
            "state": RunState.COMPLETED.value,
            "finished_at": "2026-01-01T09:00:00Z",
            "output_json": json.dumps({"score": 9999.0}),
        },
    )

    settled = settle_pending(db, now=NOW)["settled"][0]
    assert settled["gate_passed"] is None
    assert settled["accepted"] is None
    assert settled["stop_reason"] == "gate_unconfigured"
