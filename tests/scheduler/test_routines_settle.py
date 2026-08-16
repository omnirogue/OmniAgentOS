"""Tests for settling routine runs against their objective gates."""

from __future__ import annotations

import json
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import RunState
from omniagentos.db.store import SqliteStore
from omniagentos.policy import load_policy
from omniagentos.scheduler import routines_settle as routines_settle_module
from omniagentos.scheduler.gate_evidence import (
    MERGE_GATE_ROUTINE_ID,
    MERGE_GATE_TYPE,
    GateEvidence,
    GateEvidenceStore,
    binding_digest,
    normalize_gate_command,
    verify_candidate_receipt,
    workspace_digest_for,
)
from omniagentos.scheduler.gate_runner import GateEvidenceOutcome, GateRunRequest
from omniagentos.scheduler.routines import ADVERSE_STOP_REASONS, GATE_TYPES, validate_routine
from omniagentos.scheduler.routines_settle import (
    EXECUTED_GATE_TYPES,
    evaluate_gate,
    settle_pending,
)
from omniagentos.scheduler.routines_tick import tick
from omniagentos.scheduler.store import RoutinesStore
from tests.routines.conftest import valid_routine_payload
from tests.support.db_template import make_store

NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
FINISHED_AT = "2026-01-01T09:01:00Z"


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "routines_settle.db")


@pytest.fixture
def routines(database: SqliteStore) -> RoutinesStore:
    return RoutinesStore(database)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
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


@pytest.fixture
def evidence_store(tmp_path: Path) -> GateEvidenceStore:
    return GateEvidenceStore(tmp_path / "gate-evidence")


def _evidence_for(request: GateRunRequest, **overrides: Any) -> GateEvidence:
    """Evidence that is correct for *request* except where an override breaks it."""
    command = normalize_gate_command(str(request.gate_config["command"]))
    targets = tuple(overrides.pop("targets", ("tests",)))
    workspace_digest = str(
        overrides.pop("workspace_digest", workspace_digest_for(request.workspace))
    )
    candidate_sha = str(overrides.pop("candidate_sha", request.candidate_sha or ""))
    merge_base_sha = str(overrides.pop("merge_base_sha", request.merge_base_sha or ""))
    workspace_sha = str(
        overrides.pop(
            "workspace_sha",
            candidate_sha if candidate_sha else "a" * 40,
        )
    )
    stamp = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    fields: dict[str, Any] = {
        # Independent wire literal so a SCHEMA revert to v2 fails this suite.
        "schema": "omniagentos.gate-evidence.v3",
        "routine_id": request.routine_id,
        "run_id": request.run_id,
        "iteration": int(request.iteration),
        "gate_type": request.gate_type,
        "command": command,
        "targets": targets,
        "workspace_digest": workspace_digest,
        "binding_digest": binding_digest(
            routine_id=request.routine_id,
            run_id=request.run_id,
            iteration=int(request.iteration),
            gate_type=request.gate_type,
            command=command,
            targets=targets,
            workspace_digest=workspace_digest,
            candidate_sha=candidate_sha,
            merge_base_sha=merge_base_sha,
        ),
        "tool": "pytest",
        "tool_version": "9.9.9",
        "exit_code": 0,
        "checks_collected": 3,
        "checks_passed": 3,
        "checks_skipped": 0,
        "checks_failed": 0,
        "started_at": stamp,
        "finished_at": stamp,
        "nonce": secrets.token_hex(16),
        "workspace_sha": workspace_sha,
        "workspace_tree_clean": bool(overrides.pop("workspace_tree_clean", True)),
        "interpreter": str(overrides.pop("interpreter", "/usr/bin/python3")),
        "interpreter_version": str(overrides.pop("interpreter_version", "3.12.0")),
        "node_inventory_digest": str(overrides.pop("node_inventory_digest", "0" * 64)),
        "deselected_count": int(overrides.pop("deselected_count", 0)),
        "candidate_sha": candidate_sha,
        "merge_base_sha": merge_base_sha,
    }
    fields.update(overrides)
    return GateEvidence(**fields)


class _StubGateRunner:
    """Mints evidence for whatever the settler asks about."""

    def __init__(self, store: GateEvidenceStore, **overrides: Any) -> None:
        self.store = store
        self.overrides = overrides
        self.calls = 0
        self.last_request: GateRunRequest | None = None

    def run(self, request: GateRunRequest) -> GateEvidence:
        self.calls += 1
        self.last_request = request
        return self.store.record(_evidence_for(request, **self.overrides))


def _fire(
    database: SqliteStore,
    routines: RoutinesStore,
    **routine_overrides: object,
) -> tuple[dict, dict]:
    payload = valid_routine_payload(
        trigger_config={"cron": "* * * * *"},
        task_template={"title": "Settle me", "harness": "mock"},
    )
    payload.update(routine_overrides)
    routine = routines.create_routine(payload)
    fired = tick(database, load_policy(), now=NOW)["fired"][0]
    routine_run = routines.list_runs(routine["id"])[0]
    assert routine_run["run_id"] == fired["run_id"]
    return routine, routine_run


def test_completed_run_settles_as_accepted_and_updates_rollups(
    database: SqliteStore,
    routines: RoutinesStore,
    evidence_store: GateEvidenceStore,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    routine, routine_run = _fire(database, routines)
    assert database.update_run(
        routine_run["run_id"],
        {
            "state": RunState.COMPLETED.value,
            "finished_at": FINISHED_AT,
            "cost_usd": 1.25,
            "output_json": json.dumps({"exit_code": 0}),
        },
    )
    runner = _StubGateRunner(evidence_store)

    result = settle_pending(
        database,
        now=NOW,
        evidence_store=evidence_store,
        gate_runner=runner,
        workspace=workspace,
    )

    assert result["checked"] == 1
    assert result["errors"] == []
    settled = result["settled"][0]
    assert settled["gate_passed"] is True
    assert settled["accepted"] is True
    assert settled["stop_reason"] == "gate_passed"
    assert settled["finished_at"] == FINISHED_AT

    updated_routine = routines.get_routine(routine["id"])
    assert updated_routine is not None
    assert updated_routine["total_runs"] == 1
    assert updated_routine["accepted_runs"] == 1
    assert updated_routine["acceptance_rate"] == 1.0
    assert updated_routine["total_cost_usd"] == 1.25
    assert updated_routine["cost_per_accepted_change"] == 1.25
    assert runner.calls == 1

    second_result = settle_pending(
        database,
        now=NOW,
        evidence_store=evidence_store,
        gate_runner=runner,
        workspace=workspace,
    )
    assert second_result == {"checked": 0, "settled": [], "errors": []}
    assert runner.calls == 1


def test_completed_run_with_unreported_cost_poisons_total_cost_usd(
    database: SqliteStore,
    routines: RoutinesStore,
    evidence_store: GateEvidenceStore,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISSUE-8 end-to-end: a dispatched run whose provider never reported a
    cost (``runs.cost_usd`` left NULL) must settle with the routine's
    ``total_cost_usd`` UNKNOWN, never a manufactured ``$0.00``."""
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    routine, routine_run = _fire(database, routines)
    assert database.update_run(
        routine_run["run_id"],
        {
            "state": RunState.COMPLETED.value,
            "finished_at": FINISHED_AT,
            # Deliberately no "cost_usd" key: the provider never reported one,
            # so `runs.cost_usd` stays NULL — genuinely unknown, not free.
            "output_json": json.dumps({"exit_code": 0}),
        },
    )
    runner = _StubGateRunner(evidence_store)

    result = settle_pending(
        database,
        now=NOW,
        evidence_store=evidence_store,
        gate_runner=runner,
        workspace=workspace,
    )

    assert result["checked"] == 1
    settled = result["settled"][0]
    assert settled["gate_passed"] is True
    assert settled["accepted"] is True

    updated_routine = routines.get_routine(routine["id"])
    assert updated_routine is not None
    assert updated_routine["total_runs"] == 1
    assert updated_routine["accepted_runs"] == 1
    assert updated_routine["total_cost_usd"] is None
    assert updated_routine["cost_per_accepted_change"] is None

    # Sol review, seam 2: the CHILD audit row (routine_runs.cost_usd,
    # nullable since migration 120) must ALSO read unknown — not the
    # provisional $0.00 record_run wrote at fire time — or
    # list_runs/list_recent_runs (and the dashboard's RecentRunsPanel) would
    # still serve a manufactured zero for this exact run.
    child_rows = routines.list_runs(routine["id"])
    assert child_rows[0]["cost_usd"] is None
    recent = routines.list_recent_runs()
    assert recent[0]["cost_usd"] is None


def test_completed_run_without_trusted_evidence_fails_closed(
    database: SqliteStore, routines: RoutinesStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_GATE_WORKSPACE", raising=False)
    routine, routine_run = _fire(database, routines)
    assert database.update_run(
        routine_run["run_id"],
        {
            "state": RunState.COMPLETED.value,
            "finished_at": FINISHED_AT,
            "output_json": json.dumps(
                {
                    "exit_code": 0,
                    "gate_evidence": {
                        "command": "ruff check .",
                        "exit_code": 0,
                        "checks_run": 12,
                        "checks_passed": 12,
                        "checks_skipped": 0,
                        "checks_failed": 0,
                    },
                }
            ),
        },
    )

    settled = settle_pending(database, now=NOW)["settled"][0]

    assert settled["gate_passed"] is None
    assert settled["accepted"] is None
    assert settled["stop_reason"] == "gate_evidence_unavailable"
    assert routines.get_routine(routine["id"])["accepted_runs"] == 0  # type: ignore[index]


def test_settlement_without_gate_workspace_never_creates_evidence_state(
    database: SqliteStore,
    routines: RoutinesStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_GATE_WORKSPACE", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    _routine, routine_run = _fire(database, routines)
    assert database.update_run(
        routine_run["run_id"],
        {
            "state": RunState.COMPLETED.value,
            "finished_at": FINISHED_AT,
            "output_json": json.dumps({"exit_code": 0}),
        },
    )

    assert settle_pending(database, now=NOW)["settled"][0]["gate_passed"] is None

    assert not (tmp_path / "var" / "gate-evidence").exists()


def test_failed_run_settles_with_failed_gate(
    database: SqliteStore, routines: RoutinesStore
) -> None:
    routine, routine_run = _fire(database, routines)
    assert database.update_run(
        routine_run["run_id"],
        {
            "state": RunState.FAILED.value,
            "finished_at": FINISHED_AT,
            "error": "boom",
        },
    )

    result = settle_pending(database, now=NOW)

    settled = result["settled"][0]
    assert settled["gate_passed"] is False
    assert settled["accepted"] is False
    assert settled["stop_reason"] == "run_failed"
    assert routines.get_routine(routine["id"])["accepted_runs"] == 0  # type: ignore[index]


def test_refused_gate_settles_as_explicit_adverse_reason(
    database: SqliteStore,
    routines: RoutinesStore,
    evidence_store: GateEvidenceStore,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "gate_refused" in ADVERSE_STOP_REASONS
    routine, routine_run = _fire(database, routines)
    assert database.update_run(
        routine_run["run_id"],
        {
            "state": RunState.COMPLETED.value,
            "finished_at": FINISHED_AT,
            "output_json": json.dumps({"exit_code": 0}),
        },
    )
    monkeypatch.setattr(
        routines_settle_module,
        "produce_gate_evidence",
        lambda *_args, **_kwargs: GateEvidenceOutcome(
            status="refused", evidence=None, detail="test refusal"
        ),
    )

    settled = settle_pending(
        database,
        now=NOW,
        evidence_store=evidence_store,
        workspace=workspace,
    )["settled"][0]

    assert settled["stop_reason"] == "gate_refused"
    assert settled["stop_reason"] in ADVERSE_STOP_REASONS
    assert settled["gate_passed"] is False
    assert settled["accepted"] is False


def test_metric_threshold_settlement(
    database: SqliteStore,
    routines: RoutinesStore,
) -> None:
    """Per contract §7: metric_threshold gates settle gate_unverifiable."""
    _routine, routine_run = _fire(
        database,
        routines,
        gate_type="metric_threshold",
        gate_config={"metric": "quality.score", "operator": ">=", "threshold": 0.9},
        task_template={"title": "Settle metric", "harness": "cli-grok"},
    )
    assert database.update_run(
        routine_run["run_id"],
        {
            "state": RunState.COMPLETED.value,
            "finished_at": FINISHED_AT,
            "output_json": json.dumps({"quality": {"score": 0.95}}),
        },
    )

    settled = settle_pending(database, now=NOW)["settled"][0]

    assert settled["gate_passed"] is None
    assert settled["accepted"] is None
    assert settled["stop_reason"] == "gate_unconfigured"


@pytest.mark.parametrize(
    "output",
    [
        pytest.param({"exit_code": 0}, id="self-reported-exit-code"),
        pytest.param('{"exit_code": 0}', id="self-reported-exit-code-string"),
        pytest.param({}, id="empty-output"),
        pytest.param(None, id="no-output"),
        pytest.param(
            {
                "gate_evidence": {
                    "command": "pytest tests/routines",
                    "exit_code": 0,
                    "checks_run": 10,
                    "checks_passed": 10,
                }
            },
            id="hand-written-gate-evidence",
        ),
    ],
)
@pytest.mark.parametrize("gate_type", ["exit_code", "test_command"])
def test_executed_gates_never_believe_the_graded_run(gate_type: str, output: object) -> None:
    assert not evaluate_gate(
        gate_type,
        {"command": "pytest tests/routines", "expected_exit_code": 0},
        {"id": "run-1", "state": "completed", "output_json": output},
        evidence_store=None,
    )


def test_executed_gate_accepts_only_matching_trusted_evidence(
    evidence_store: GateEvidenceStore, workspace: Path
) -> None:
    request = GateRunRequest(
        routine_id="rt-1",
        run_id="run-1",
        iteration=2,
        gate_type="test_command",
        gate_config={"command": "pytest tests", "expected_exit_code": 0},
        workspace=workspace,
    )
    evidence = evidence_store.record(_evidence_for(request))

    assert evaluate_gate(
        "test_command",
        request.gate_config,
        {"id": "run-1", "state": "completed", "output_json": None},
        evidence=evidence,
        routine_id="rt-1",
        iteration=2,
        workspace_digest=workspace_digest_for(workspace),
        now=NOW,
        evidence_store=evidence_store,
    )
    assert not evaluate_gate(
        "test_command",
        request.gate_config,
        {"id": "run-1", "state": "cancelled", "output_json": None},
        evidence=evidence,
        routine_id="rt-1",
        iteration=2,
        workspace_digest=workspace_digest_for(workspace),
        now=NOW,
        evidence_store=evidence_store,
    )


@pytest.mark.parametrize(
    ("label", "overrides", "context"),
    [
        ("wrong routine", {}, {"routine_id": "rt-other"}),
        ("wrong iteration", {}, {"iteration": 3}),
        ("wrong workspace", {"workspace_digest": "deadbeef"}, {}),
        ("wrong gate type", {"gate_type": "exit_code"}, {}),
        ("wrong command", {"command": "pytest tests/other"}, {}),
        ("tampered binding digest", {"binding_digest": "0" * 64}, {}),
        ("wrong schema", {"schema": "omniagentos.gate-evidence.v0"}, {}),
        ("nonzero exit", {"exit_code": 1}, {}),
        ("no targets", {"targets": ()}, {}),
        ("vacuous pass", {"checks_collected": 0, "checks_passed": 0}, {}),
        ("failed checks", {"checks_failed": 1, "checks_passed": 2}, {}),
        ("unaccounted skipped checks", {"checks_skipped": 1, "checks_passed": 1}, {}),
        ("partial execution", {"checks_passed": 2}, {}),
        ("negative counts", {"checks_passed": -3, "checks_collected": -3}, {}),
        ("unidentified tool", {"tool_version": ""}, {}),
        ("weak nonce", {"nonce": "abc"}, {}),
        ("unparseable timestamps", {"finished_at": "not-a-time"}, {}),
        ("finished before started", {"started_at": "2026-01-01T09:30:00Z"}, {}),
        ("stale evidence", {"finished_at": "2025-12-30T09:00:00Z"}, {}),
        ("future evidence", {"finished_at": "2026-01-01T10:00:00Z"}, {}),
    ],
)
def test_executed_gate_rejects_mismatched_or_vacuous_evidence(
    evidence_store: GateEvidenceStore,
    workspace: Path,
    label: str,
    overrides: dict[str, Any],
    context: dict[str, Any],
) -> None:
    request = GateRunRequest(
        routine_id="rt-1",
        run_id="run-1",
        iteration=2,
        gate_type="test_command",
        gate_config={"command": "pytest tests", "expected_exit_code": 0},
        workspace=workspace,
    )
    evidence = evidence_store.sign(_evidence_for(request, **overrides))

    assert not evaluate_gate(
        "test_command",
        request.gate_config,
        {"id": "run-1", "state": "completed", "output_json": None},
        evidence=evidence,
        routine_id=str(context.get("routine_id", "rt-1")),
        iteration=int(context.get("iteration", 2)),
        workspace_digest=workspace_digest_for(workspace),
        now=NOW,
        evidence_store=evidence_store,
    ), label


def test_executed_gate_rejects_evidence_replayed_onto_another_run(
    evidence_store: GateEvidenceStore, workspace: Path
) -> None:
    first = GateRunRequest(
        routine_id="rt-1",
        run_id="run-1",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": "pytest tests", "expected_exit_code": 0},
        workspace=workspace,
    )
    evidence = evidence_store.record(_evidence_for(first))

    assert not evaluate_gate(
        "test_command",
        first.gate_config,
        {"id": "run-2", "state": "completed", "output_json": None},
        evidence=evidence,
        routine_id="rt-1",
        iteration=1,
        workspace_digest=workspace_digest_for(workspace),
        now=NOW,
        evidence_store=evidence_store,
    )


def test_tick_non_dry_run_includes_settlement_summary(
    database: SqliteStore, routines: RoutinesStore
) -> None:
    routines.create_routine(
        valid_routine_payload(
            trigger_config={"cron": "* * * * *"},
            task_template={"title": "Tick settlement", "harness": "mock"},
        )
    )

    result = tick(database, load_policy(), now=NOW)

    assert "settled" in result
    assert result["settled"] == {"checked": 0, "settled": [], "errors": []}


def test_tick_threads_template_task_mode_into_task_input(
    database: SqliteStore, routines: RoutinesStore
) -> None:
    routines.create_routine(
        valid_routine_payload(
            trigger_config={"cron": "* * * * *"},
            task_template={
                "title": "Report task",
                "harness": "mock",
                "task_mode": "report",
                "input": {"topic": "reliability"},
            },
        )
    )

    fired = tick(database, load_policy(), now=NOW)["fired"][0]

    task = database.get_task(fired["task_id"])
    assert task is not None
    assert json.loads(task["input_json"]) == {
        "task_mode": "report",
        "topic": "reliability",
    }


def test_email_notification_without_environment_never_raises(
    database: SqliteStore,
    routines: RoutinesStore,
    evidence_store: GateEvidenceStore,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PIEDPIPER_ACMEUNI_TOKEN", raising=False)
    monkeypatch.delenv("PIEDPIPER_ACMEUNI_LOCATION_ID", raising=False)
    _routine, routine_run = _fire(
        database,
        routines,
        notification_target={"channel": "email", "target": "ops@example.com"},
    )
    assert database.update_run(
        routine_run["run_id"],
        {
            "state": RunState.COMPLETED.value,
            "finished_at": FINISHED_AT,
            "output_json": json.dumps({"exit_code": 0}),
        },
    )

    result = settle_pending(
        database,
        now=NOW,
        evidence_store=evidence_store,
        gate_runner=_StubGateRunner(evidence_store),
        workspace=workspace,
    )

    assert result["errors"] == []
    assert result["settled"][0]["accepted"] is True


def test_merge_candidate_is_a_first_class_scheduler_gate_type() -> None:
    """Production GATE_TYPES / EXECUTED_GATE_TYPES must accept merge_candidate.

    Failing-on-revert: dropping merge_candidate from either frozenset makes this
    fail without following a production import of the allowed set into the
    expected value.
    """
    assert "merge_candidate" in GATE_TYPES
    assert MERGE_GATE_TYPE in EXECUTED_GATE_TYPES
    assert MERGE_GATE_TYPE == "merge_candidate"

    candidate = "a" * 40
    merge_base = "b" * 40
    validate_routine(
        valid_routine_payload(
            id=MERGE_GATE_ROUTINE_ID,
            name="merge-gate-routine",
            gate_type=MERGE_GATE_TYPE,
            gate_config={
                "command": "pytest tests",
                "expected_exit_code": 0,
                "candidate_sha": candidate,
                "merge_base_sha": merge_base,
            },
        )
    )


def test_settle_path_produces_verifiable_merge_candidate_receipt(
    database: SqliteStore,
    routines: RoutinesStore,
    evidence_store: GateEvidenceStore,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sole production GateRunRequest site must mint a merge-gate receipt.

    Proves candidate_sha / merge_base_sha leave routines_settle into the runner
    and land under records/merge-gate/<candidate>.json — the path merge-gate.sh
    consumes. Direct runner construction alone is not enough.
    """
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    workspace_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    candidate_sha = workspace_sha
    merge_base_sha = workspace_sha

    routine, routine_run = _fire(
        database,
        routines,
        id=MERGE_GATE_ROUTINE_ID,
        name="merge-gate-settle",
        gate_type=MERGE_GATE_TYPE,
        gate_config={
            "command": "pytest tests",
            "expected_exit_code": 0,
            "candidate_sha": candidate_sha,
            "merge_base_sha": merge_base_sha,
        },
    )
    assert routine["id"] == MERGE_GATE_ROUTINE_ID
    assert database.update_run(
        routine_run["run_id"],
        {
            "state": RunState.COMPLETED.value,
            "finished_at": FINISHED_AT,
            "output_json": json.dumps({"exit_code": 0}),
        },
    )
    runner = _StubGateRunner(evidence_store)

    result = settle_pending(
        database,
        now=NOW,
        evidence_store=evidence_store,
        gate_runner=runner,
        workspace=workspace,
    )

    assert result["errors"] == []
    settled = result["settled"][0]
    assert settled["gate_passed"] is True
    assert settled["stop_reason"] == "gate_passed"
    assert runner.calls == 1
    assert runner.last_request is not None
    assert runner.last_request.candidate_sha == candidate_sha
    assert runner.last_request.merge_base_sha == merge_base_sha
    assert runner.last_request.gate_type == MERGE_GATE_TYPE
    assert runner.last_request.routine_id == MERGE_GATE_ROUTINE_ID
    assert runner.last_request.run_id == candidate_sha

    receipt_path = evidence_store.root / "records" / MERGE_GATE_ROUTINE_ID / f"{candidate_sha}.json"
    assert receipt_path.is_file()
    evidence = verify_candidate_receipt(
        receipt_path,
        evidence_root=evidence_store.root,
        candidate_sha=candidate_sha,
        merge_base_sha=merge_base_sha,
        now=NOW,
    )
    assert evidence.schema == "omniagentos.gate-evidence.v3"
    assert evidence.gate_type == MERGE_GATE_TYPE
    assert evidence.candidate_sha == candidate_sha
    assert evidence.merge_base_sha == merge_base_sha
    assert evidence.routine_id == MERGE_GATE_ROUTINE_ID
    assert evidence.run_id == candidate_sha


def test_run_with_unexecuted_gate_type_settles_with_null_gate_passed(
    database: SqliteStore, routines: RoutinesStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED: run with gate type lacking trusted evidence path leaves gate_passed NULL.

    When a routine has gate_type that is NOT in EXECUTED_GATE_TYPES (e.g., metric_threshold),
    settlement must leave gate_passed and accepted NULL so _count_settled_runs
    excludes it from the denominator (no contribution to acceptance floor).
    """
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    # Create routine with metric_threshold gate (not in EXECUTED_GATE_TYPES)
    # Use cli-grok harness to satisfy D5 validation
    routine = routines.create_routine(
        valid_routine_payload(
            trigger_config={"cron": "* * * * *"},  # Every minute
            gate_type="metric_threshold",
            gate_config={"metric": "quality.score", "operator": ">=", "threshold": 0.9},
            task_template={"title": "Metric test", "harness": "cli-grok"},
        )
    )
    assert tick(database, load_policy(), now=NOW)["fired"], "routine must fire"
    routine_run = routines.list_runs(routine["id"])[0]

    assert database.update_run(
        routine_run["run_id"],
        {
            "state": RunState.COMPLETED.value,
            "finished_at": FINISHED_AT,
            "cost_usd": 1.0,
            "output_json": json.dumps({"quality": {"score": 0.95}}),
        },
    )

    settled = settle_pending(database, now=NOW)["settled"][0]

    # Key assertions: gate_passed and accepted must be NULL
    assert settled["gate_passed"] is None
    assert settled["accepted"] is None
    assert settled["stop_reason"] == "gate_unconfigured"
    assert "excluded from acceptance floor" in settled["notes"]

    # Verify it was not counted in acceptance_runs
    updated_routine = routines.get_routine(routine["id"])
    assert updated_routine is not None
    assert updated_routine["total_runs"] == 1
    assert updated_routine["accepted_runs"] == 0  # NOT incremented


def test_three_unexecuted_gate_runs_do_not_trigger_auto_pause(
    database: SqliteStore, routines: RoutinesStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED: 3 runs with unexecuted gate type must NOT auto-pause the routine.

    The acceptance floor requires settled runs (where gate_passed IS NOT NULL).
    Runs with no trusted evidence path are excluded from the denominator,
    so 3 such runs should never trigger auto-pause even with gate_passed=NULL.
    """
    from datetime import timedelta

    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    routine = routines.create_routine(
        valid_routine_payload(
            trigger_config={"cron": "* * * * *"},  # Every minute
            gate_type="metric_threshold",
            gate_config={"metric": "quality.score", "operator": ">=", "threshold": 0.9},
            task_template={"title": "Metric pause test", "harness": "cli-grok"},
        )
    )

    # Fire and settle 3 runs, all with unexecuted gate
    # Use different NOW timestamps to allow the routine to fire again
    for i in range(3):
        tick_now = NOW + timedelta(minutes=i + 1)
        assert tick(database, load_policy(), now=tick_now)["fired"], "routine must fire"
        routine_run = routines.list_runs(routine["id"])[0]
        assert database.update_run(
            routine_run["run_id"],
            {
                "state": RunState.COMPLETED.value,
                "finished_at": tick_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cost_usd": 1.0,
                "output_json": json.dumps({"quality": {"score": 0.95}}),
            },
        )
        settle_pending(database, now=tick_now)

    # Routine must still be active (not auto-paused)
    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["status"] == "active"
    assert updated["auto_pause_reason"] == ""


def test_doctrine_unexecuted_gate_never_affects_acceptance_rate(
    database: SqliteStore, routines: RoutinesStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DOCTRINE: gate types without trusted evidence path never contribute to acceptance rate.

    This test articulates the invariant: acceptance_rate is only affected by runs
    where an executed gate (with trusted evidence) was evaluated. Runs with
    unexecuted gate types are completely excluded from the denominator, as if
    they never happened (except for cost tracking).
    """
    from datetime import timedelta

    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    routine = routines.create_routine(
        valid_routine_payload(
            trigger_config={"cron": "* * * * *"},  # Every minute
            gate_type="metric_threshold",
            gate_config={"metric": "quality.score", "operator": ">=", "threshold": 0.9},
            task_template={"title": "Doctrine metric", "harness": "cli-grok"},
        )
    )

    # Settle 5 runs with unexecuted gate
    # Use different NOW timestamps to allow the routine to fire again
    for i in range(5):
        tick_now = NOW + timedelta(minutes=i + 1)
        assert tick(database, load_policy(), now=tick_now)["fired"], "routine must fire"
        routine_run = routines.list_runs(routine["id"])[0]
        assert database.update_run(
            routine_run["run_id"],
            {
                "state": RunState.COMPLETED.value,
                "finished_at": tick_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cost_usd": 1.0,
                "output_json": json.dumps({"quality": {"score": 0.95}}),
            },
        )
        settle_pending(database, now=tick_now)

    # Acceptance metrics must reflect "no gates settled", not 0/5
    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["total_runs"] == 5  # Total count goes up
    assert updated["accepted_runs"] == 0  # But accepted stays 0 (no settled gates)

    # The real invariant is that _count_settled_runs returns 0 settled runs
    from omniagentos.scheduler.store import _count_settled_runs

    settled_runs, settled_accepted = _count_settled_runs(
        database._connection, routine["id"], limit=100
    )
    assert settled_runs == 0, "unexecuted-gate runs must not be counted as settled"
    assert settled_accepted == 0


def test_run_with_gate_workspace_none_settles_with_null_gate_passed(
    database: SqliteStore, routines: RoutinesStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED: run with gate_workspace=None (no configured workspace) leaves gate_passed NULL.

    This is the LIVE path that was broken: gate_type IS configured (e.g., exit_code),
    but gate_workspace is None, so gate cannot execute. This is absence of evidence,
    not a rejection. Should NOT enter acceptance floor denominator.
    """
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_GATE_WORKSPACE", raising=False)
    # Use default routine with exit_code gate (EXECUTED_GATE_TYPE)
    routine, routine_run = _fire(database, routines)
    assert database.update_run(
        routine_run["run_id"],
        {
            "state": RunState.COMPLETED.value,
            "finished_at": FINISHED_AT,
            "cost_usd": 1.0,
        },
    )

    settled = settle_pending(database, now=NOW)["settled"][0]

    # Key assertion: gate_passed and accepted must be NULL (not False)
    assert settled["gate_passed"] is None
    assert settled["accepted"] is None
    assert settled["stop_reason"] == "gate_evidence_unavailable"
    assert "no configured gate workspace" in settled["notes"]
    assert "excluded from acceptance floor" in settled["notes"]

    # Verify it was not counted in acceptance_runs
    updated_routine = routines.get_routine(routine["id"])
    assert updated_routine is not None
    assert updated_routine["total_runs"] == 1
    assert updated_routine["accepted_runs"] == 0  # NOT incremented
