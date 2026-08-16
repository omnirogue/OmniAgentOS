"""End-to-end intake loop: dispatch a spec -> live board card + run -> runner
executes it via an agent -> the card moves To-Do -> In-Progress -> Done."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.contracts import (
    BudgetDecision,
    HarnessType,
    PolicyDecision,
    SandboxSpec,
)
from omniagentos.intake.contracts import RefinedSpec
from omniagentos.intake.service import dispatch_spec, reconcile_board
from omniagentos.mock_adapter import MockAdapter
from omniagentos.policy import load_policy
from omniagentos.runner.core import Runner, RunnerDependencies


def _runner_deps(adapter: MockAdapter) -> RunnerDependencies:
    return RunnerDependencies(
        evaluate_policy=lambda action: PolicyDecision(requires_approval=False, always_human=False),
        sandbox_for_tools=lambda harness, tools: SandboxSpec(level="read_only"),
        check_budget=lambda spec, w, t, c: BudgetDecision(allowed=True),
        resolve_adapter=lambda harness: adapter,
        append_manifest=lambda root, manifest: str(Path(root) / "runs.jsonl"),
        render_run_note=lambda run, steps, manifest_path, receipts, **kw: (
            f"runs/{run['id']}.md",
            "note",
        ),
        write_note=lambda root, relpath, content: str(Path(root) / relpath),
    )


def _find(board: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
    return next(t for t in board if t["id"] == task_id)


def test_dispatch_flows_to_done(tmp_path: Path) -> None:
    db = str(tmp_path / "loop.db")
    collab = CollabStore(db)
    store = collab._store  # same DB: board_tasks + runs coexist for reconciliation.
    policy = load_policy()

    spec = RefinedSpec(
        title="Add a healthcheck endpoint",
        description="Expose GET /health returning 200.",
        acceptance_criteria=["returns 200", "documented"],
        suggested_priority="high",
    )
    result = dispatch_spec(store, collab, policy, spec, harness=HarnessType.MOCK.value)
    board_id = str(result["board_task"]["id"])
    run_id = str(result["run_id"])

    # A dispatched task lands in To-Do (open) and is linked to its executing run.
    assert result["board_task"]["status"] == "open"
    assert result["board_task"]["run_id"] == run_id
    assert result["board_task"]["priority"] == "high"

    board = reconcile_board(store, collab)
    assert _find(board, board_id)["status"] == "open"

    # The runner's agent claims and executes the run.
    runner = Runner(
        store,
        "w-intake",
        dependencies=_runner_deps(MockAdapter()),
        ledger_dir=str(tmp_path / "ledger"),
        vault_dir=str(tmp_path / "vault"),
        workspace_base=str(tmp_path / "ws"),
    )
    for _ in range(20):
        if not runner.tick():
            break

    # The card has moved to Done, linked to the completed run.
    board = reconcile_board(store, collab)
    row = _find(board, board_id)
    assert row["status"] == "done"
    assert row["result_ref"] == run_id
    assert row["run_state"] == "completed"

    # The control-plane run itself completed.
    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == "completed"
    assert run["priority"] == 1


@pytest.mark.parametrize(
    ("board_priority", "run_priority"),
    [("urgent", 0), ("high", 1), ("normal", 2), ("low", 3)],
)
def test_board_priority_drives_run_queue_priority(
    tmp_path: Path, board_priority: str, run_priority: int
) -> None:
    collab = CollabStore(str(tmp_path / f"board-priority-{board_priority}.db"))
    result = dispatch_spec(
        collab._store,
        collab,
        load_policy(),
        RefinedSpec(title=f"{board_priority} work", suggested_priority=board_priority),
        harness=HarnessType.MOCK.value,
    )
    run = collab._store.get_run(str(result["run_id"]))
    assert run is not None
    assert result["board_task"]["priority"] == board_priority
    assert run["priority"] == run_priority


def test_probe_dispatch_creates_a_prearchived_reconcilable_card(tmp_path: Path) -> None:
    db = str(tmp_path / "probe.db")
    collab = CollabStore(db)
    result = dispatch_spec(
        collab._store,
        collab,
        load_policy(),
        RefinedSpec(title="  PrObE  ", description="health check"),
        harness=HarnessType.MOCK.value,
    )
    task_id = str(result["board_task"]["id"])

    archived = collab.get_board_task(task_id)
    assert archived is not None and archived["archived"] is True
    # Reconciliation deliberately still updates archived cards when requested.
    rows = reconcile_board(collab._store, collab, archived=1)
    assert _find(rows, task_id)["run_id"] == result["run_id"]


def test_session_linked_card_reconciles_from_session_state(tmp_path: Path) -> None:
    """A fast-lane / execute="session" card links a live session via result_ref (a
    ses_ id) and has NO run. The board must project the SESSION's state onto the card
    (To-Do -> In-Progress -> Done), else a completed fast task is stuck in To-Do."""
    from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
    from omniagentos.contracts import utc_now_iso
    from omniagentos.sessions.dal import SessionsDal, SessionState

    db = str(tmp_path / "loop.db")
    collab = CollabStore(db)
    store = collab._store
    sdal = SessionsDal(db)

    session_id = "ses_fastlanecard0001"
    now = utc_now_iso()
    sdal.create_session(
        {
            "id": session_id,
            "source": "bridge",
            "project_dir": str(tmp_path),
            "provider": "claude",
            "session_ref": "ref-fastlane-1",
            "state": SessionState.RUNNING.value,
            "model": "claude-fable-5",
            "title": "make a folder on my desktop called tiger",
            "budget_usd_max": None,
            "cost_usd": 0.0,
            "kill_requested": 0,
            "last_activity_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    card = BoardTask(
        title="make a folder on my desktop called tiger",
        description="",
        status=BoardTaskStatus.OPEN,
        result_ref=session_id,
    )
    collab.create_board_task(card)

    # A RUNNING session moves the card into In-Progress.
    board = reconcile_board(store, collab, sessions_dal=sdal)
    row = _find(board, card.id)
    assert row["status"] == "in_progress"
    assert row["run_state"] == "running"

    # When the session completes, the card moves to Done.
    assert sdal.update_session_state(
        session_id, SessionState.COMPLETED.value, expect=SessionState.RUNNING.value
    )
    board = reconcile_board(store, collab, sessions_dal=sdal)
    row = _find(board, card.id)
    assert row["status"] == "done"
    assert row["run_state"] == "completed"
    sdal.close()


def test_hand_created_card_without_run_is_untouched(tmp_path: Path) -> None:
    from omniagentos.collab.contracts import BoardTask

    collab = CollabStore(str(tmp_path / "board.db"))
    store = collab._store
    card = BoardTask(title="Manual card", description="no run linked")
    collab.create_board_task(card)

    board = reconcile_board(store, collab)
    row = _find(board, card.id)
    assert row["status"] == "open"
    assert row["run_id"] is None
    assert row["run_state"] is None
