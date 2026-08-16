from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import omniagentos.api.main  # noqa: F401  -- break the package's documented import cycle.
from omniagentos.api.routes import intake as intake_routes
from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import HarnessType
from omniagentos.intake import service
from omniagentos.intake.contracts import RefinedSpec
from omniagentos.intake.fastlane import target_and_todo_prompt
from omniagentos.intake.orchestrations import OrchestrationsDal
from omniagentos.intake.service import create_queued_goal_card, dispatch_spec, reconcile_board
from omniagentos.notifications.dal import NotificationsDal
from omniagentos.policy import load_policy

DELIVERABLES = (
    "Deliverables: write final output files into the `outputs/` folder inside your working "
    "directory (it already exists). Only write elsewhere if the user explicitly names a "
    "destination. Files a user gave you are in `uploads/`."
)


@dataclass
class _Result:
    run_id: str
    status: str = "done"
    tasks: list[Any] | None = None
    escalations: list[Any] | None = None


def _wait_for(dal: OrchestrationsDal, orch_id: str, state: str) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        row = dal.get(orch_id)
        if row is not None and row["status"] == state:
            return row
        time.sleep(0.01)
    raise AssertionError(f"orchestration {orch_id} never reached {state}")


def test_queued_orchestration_is_live_on_board_then_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    db = str(tmp_path / "lifecycle.db")
    collab = CollabStore(db)
    release = threading.Event()
    runner_started = threading.Event()
    card = create_queued_goal_card(collab, "Run a long task", "orch_lifecycle")

    def runner(_goal: str, **kwargs: Any) -> _Result:
        runner_started.set()
        assert release.wait(timeout=3)
        return _Result(run_id=str(kwargs["run_id"]))

    monkeypatch.setattr(
        intake_routes,
        "plan_goal",
        lambda *_args, **_kwargs: SimpleNamespace(
            project_name="Lifecycle", description="Run a long task", tasks=[]
        ),
    )
    monkeypatch.setattr(
        intake_routes,
        "dispatch_spec",
        lambda *args, **kwargs: dispatch_spec(*args, **kwargs, orchestrate_runner=runner),
    )
    intake_routes._run_quick_dispatch(
        "Run a long task",
        executor="auto",
        priority="balanced",
        plan_first=False,
        project_id=None,
        board_task_id=str(card["id"]),
        run_id="orch_lifecycle",
        store=collab._store,
        collab_store=collab,
        policy_cfg=load_policy(),
        planner_llm=lambda *_args, **_kwargs: None,
    )
    dal = OrchestrationsDal(db)
    try:
        assert runner_started.wait(timeout=3)
        row = _wait_for(dal, "orch_lifecycle", "running")
        assert row["board_task_id"] == card["id"]
        assert (Path(row["working_dir"]) / "uploads").is_dir()
        assert (Path(row["working_dir"]) / "outputs").is_dir()

        board = reconcile_board(collab._store, collab, orchestrations_dal=dal)
        board_row = next(item for item in board if item["id"] == card["id"])
        assert board_row["status"] == "in_progress"
        assert board_row["work"]["kind"] == "orchestration"
        assert board_row["work"]["state"] == "running"
        assert board_row["work"]["current_step"] == "running"

        release.set()
        _wait_for(dal, "orch_lifecycle", "completed")
        board = reconcile_board(collab._store, collab, orchestrations_dal=dal)
        board_row = next(item for item in board if item["id"] == card["id"])
        assert board_row["status"] == "done"
    finally:
        release.set()
        dal.close()


def _done_rows(db: str, board_task_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in NotificationsDal(db).list(limit=500)
        if row["kind"] == "done" and row["ref_id"] == board_task_id
    ]


def test_completion_emits_exactly_one_done_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C0: a finished card yields EXACTLY ONE 'done' bell, deep-linking its files.

    The orchestration-lifecycle path emits on the DONE board write; a subsequent
    ``reconcile_board`` merely OBSERVES the already-done card and must NOT
    re-bell (kind-aware, read-agnostic dedupe in ``notify_task_done``).
    """
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    db = str(tmp_path / "done-bell.db")
    collab = CollabStore(db)
    work = tmp_path / "done-ws"
    (work / "outputs").mkdir(parents=True)
    (work / "outputs" / "a.md").write_text("A", encoding="utf-8")
    (work / "outputs" / "b.md").write_text("B", encoding="utf-8")

    card = BoardTask(title="Finish the deliverable", status=BoardTaskStatus.IN_PROGRESS)
    collab.create_board_task(card)

    lifecycle = OrchestrationsDal(db)
    lifecycle.create("orch_done", board_task_id=card.id, working_dir=str(work))

    def runner(_goal: str, **kwargs: Any) -> _Result:
        return _Result(run_id=str(kwargs["run_id"]), status="done")

    # Runs synchronously in this thread: on return the DONE board write + the
    # done-notification are both persisted.
    service._run_orchestration_with_lifecycle(
        lifecycle,
        collab,
        board_id=card.id,
        run_id="orch_done",
        db_path=db,
        runner=runner,
        goal="finish it",
        priority="balanced",
        pins=None,
        working_dir=str(work),
        project_id=None,
        granted_roots=None,
    )

    assert (collab.get_board_task(card.id) or {})["status"] == "done"
    rows = _done_rows(db, card.id)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["files_count"] == 2
    assert payload["workspace"] == str(work)
    assert payload["task_title"] == "Finish the deliverable"

    # A reconcile that OBSERVES the already-done card must not add a second bell.
    orchestrations_dal = OrchestrationsDal(db)
    try:
        reconcile_board(collab._store, collab, orchestrations_dal=orchestrations_dal)
    finally:
        orchestrations_dal.close()
    assert len(_done_rows(db, card.id)) == 1


def test_synchronous_orchestration_has_live_lifecycle_and_terminal_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    db = str(tmp_path / "sync.db")
    collab = CollabStore(db)
    observer = OrchestrationsDal(db)
    seen: dict[str, Any] = {}

    def runner(_goal: str, **kwargs: Any) -> _Result:
        run_id = str(kwargs["run_id"])
        seen["row"] = observer.get(run_id)
        card = collab.list_board_tasks()[0]
        seen["card_status"] = card["status"]
        return _Result(run_id=run_id)

    try:
        result = dispatch_spec(
            collab._store,
            collab,
            load_policy(),
            RefinedSpec(title="Synchronous", description="Track inline execution"),
            execute="orchestrate",
            orchestrate_runner=runner,
        )
        row = observer.get(str(result["run_id"]))
        assert seen["row"]["status"] == "running"
        assert seen["card_status"] == "in_progress"
        assert row is not None
        assert row["status"] == "completed"
        assert row["board_task_id"] == result["board_task"]["id"]
        assert collab.get_board_task(row["board_task_id"])["status"] == "done"  # type: ignore[index]
    finally:
        observer.close()


def test_heartbeat_retries_after_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    beats: list[str] = []
    closed: list[bool] = []

    class _HeartbeatDal:
        def __init__(self, _db_path: str) -> None:
            self.calls = 0

        def heartbeat(self, run_id: str) -> None:
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError("database is busy")
            beats.append(run_id)

        def close(self) -> None:
            closed.append(True)

    class _Stop:
        def __init__(self) -> None:
            self.waits = 0

        def wait(self, _timeout: float) -> bool:
            self.waits += 1
            return self.waits > 2

    monkeypatch.setattr(service, "OrchestrationsDal", _HeartbeatDal)
    service._heartbeat_orchestration(  # type: ignore[arg-type]
        _Stop(), run_id="orch_heartbeat", db_path=":memory:"
    )
    assert beats == ["orch_heartbeat"]
    assert closed == [True]


def test_reconcile_stale_orchestration_blocks_and_marks_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "stale.db")
    collab = CollabStore(db)
    card = BoardTask(
        title="Stale orchestration",
        description="waiting",
        status=BoardTaskStatus.IN_PROGRESS,
        result_ref="orch_stale_board",
    )
    collab.create_board_task(card)
    dal = OrchestrationsDal(db)
    try:
        dal.create("orch_stale_board", board_task_id=card.id, working_dir=str(tmp_path))
        dal._connection.execute(  # noqa: SLF001 -- deterministic stale timestamp fixture.
            "UPDATE orchestrations SET heartbeat_at = '2020-01-01T00:00:00Z' WHERE id = ?",
            ("orch_stale_board",),
        )
        monkeypatch.setenv("OMNIAGENTOS_ORCH_STALE_MINUTES", "10")
        service._reset_reconcile_stale_throttle(db)
        row = next(
            item
            for item in reconcile_board(collab._store, collab, orchestrations_dal=dal)
            if item["id"] == card.id
        )
        assert row["status"] == "blocked"
        assert row["description"] == "waiting [auto-blocked: orchestrator died]"
        assert row["work"]["state"] == "failed"
        assert row["work"]["error"] == "stale heartbeat — orchestrator process died"
        events = collab._store.get_events_after(0)
        assert any(
            event["type"] == "board.updated" and event["target_id"] == card.id for event in events
        )

        second = next(
            item
            for item in reconcile_board(collab._store, collab, orchestrations_dal=dal)
            if item["id"] == card.id
        )
        assert second["description"].count(" [auto-blocked: orchestrator died]") == 1
    finally:
        dal.close()


def test_queued_orchestration_failure_is_persisted_and_blocks_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    db = str(tmp_path / "failed.db")
    collab = CollabStore(db)

    def runner(_goal: str, **_kwargs: Any) -> _Result:
        raise RuntimeError("executor crashed")

    result = dispatch_spec(
        collab._store,
        collab,
        load_policy(),
        RefinedSpec(title="Failure", description="Fail visibly"),
        execute="orchestrate",
        async_orchestrate=True,
        orchestration_run_id="orch_failed",
        orchestrate_runner=runner,
    )
    dal = OrchestrationsDal(db)
    try:
        row = _wait_for(dal, "orch_failed", "failed")
        assert row["error"] == "executor crashed"
        board = reconcile_board(collab._store, collab, orchestrations_dal=dal)
        card = next(item for item in board if item["id"] == result["board_task"]["id"])
        assert card["status"] == "blocked"
        assert card["work"]["error"] == "executor crashed"
    finally:
        dal.close()


def test_dispatch_survives_missing_orchestrations_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    db = str(tmp_path / "pre-migration.db")
    collab = CollabStore(db)
    collab._store._write("DROP TABLE orchestrations", ())

    def runner(_goal: str, **kwargs: Any) -> _Result:
        return _Result(run_id=str(kwargs["run_id"]))

    result = dispatch_spec(
        collab._store,
        collab,
        load_policy(),
        RefinedSpec(title="Legacy DB", description="Dispatch without telemetry table"),
        execute="orchestrate",
        orchestrate_runner=runner,
    )
    assert result["orchestration"]["status"] == "done"
    assert collab.get_board_task(result["board_task"]["id"])["status"] == "done"  # type: ignore[index]


def test_queued_worker_start_failure_marks_failed_and_closes_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    db = str(tmp_path / "start-failure.db")
    collab = CollabStore(db)
    created: list[Any] = []

    class _TrackingDal(OrchestrationsDal):
        def __init__(self, db_path: str) -> None:
            super().__init__(db_path)
            self.closed = False
            created.append(self)

        def close(self) -> None:
            self.closed = True
            super().close()

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(service, "OrchestrationsDal", _TrackingDal)
    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="thread unavailable"):
        dispatch_spec(
            collab._store,
            collab,
            load_policy(),
            RefinedSpec(title="Start", description="Worker start fails"),
            execute="orchestrate",
            async_orchestrate=True,
            orchestration_run_id="orch_start_failure",
            orchestrate_runner=lambda *_args, **_kwargs: pytest.fail("runner must not start"),
        )

    assert created[0].closed is True
    observer = OrchestrationsDal(db)
    try:
        row = observer.get("orch_start_failure")
        assert row is not None
        assert row["status"] == "failed"
        assert row["error"] == "thread unavailable"
        card = collab.get_board_task(row["board_task_id"])
        assert card is not None
        assert card["status"] == "blocked"
    finally:
        observer.close()


def _create_resumable_run(
    dal: OrchestrationsDal,
    card: BoardTask,
    *,
    run_id: str,
    status: str = "running",
    retry_count: int = 0,
) -> None:
    dal.create(
        run_id,
        board_task_id=card.id,
        working_dir="",
        goal="Resume the saved plan",
        params_json='{"priority":"balanced"}',
    )
    dal.record_plan(run_id, '{"saved":true}', ["Already done", "Still pending"])
    dal.step_finished(run_id, 0, "done", 1, "saved output")
    dal._connection.execute(  # noqa: SLF001 -- deterministic dead-conductor fixture.
        "UPDATE orchestrations SET status = ?, conductor_pid = 999999999, "
        "heartbeat_at = '2020-01-01T00:00:00Z', updated_at = '2020-01-01T00:00:00Z', "
        "retry_count = ? WHERE id = ?",
        (status, retry_count, run_id),
    )


def test_dead_conductor_resume_skips_done_step_and_completes_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "resume.db")
    collab = CollabStore(db)
    card = BoardTask(
        title="Resume safely",
        description="checkpointed",
        status=BoardTaskStatus.IN_PROGRESS,
        result_ref="orch_resume",
    )
    collab.create_board_task(card)
    dal = OrchestrationsDal(db)
    executed: list[int] = []

    def runner(_goal: str, **kwargs: Any) -> _Result:
        resume_state = kwargs["resume_state"]
        checkpoint = kwargs["checkpoint"]
        assert [step.status for step in resume_state.steps] == ["done", "pending"]
        for step in resume_state.steps:
            if step.status not in {"done", "unreviewed"}:
                executed.append(step.seq)
                checkpoint.step_finished("orch_resume", step.seq, "done", 1, "resumed")
        return _Result(run_id="orch_resume")

    try:
        _create_resumable_run(dal, card, run_id="orch_resume")
        monkeypatch.setattr(service, "_default_orchestrate_runner", runner)

        result = service.resume_orchestration(
            card.id,
            store=collab._store,
            collab_store=collab,
        )

        assert result == {"run_id": "orch_resume", "resumed": True, "reason": "auto"}
        _wait_for(dal, "orch_resume", "completed")
        assert executed == [1]
        board = collab.get_board_task(card.id)
        assert board is not None
        assert board["status"] == "done"
    finally:
        dal.close()


def test_failed_auto_retry_is_capped_and_manual_retry_bypasses_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "retry.db")
    collab = CollabStore(db)
    auto_card = BoardTask(
        title="Automatic retry",
        status=BoardTaskStatus.BLOCKED,
        result_ref="orch_retry_auto",
    )
    capped_card = BoardTask(
        title="Capped retry",
        status=BoardTaskStatus.BLOCKED,
        result_ref="orch_retry_capped",
    )
    collab.create_board_task(auto_card)
    collab.create_board_task(capped_card)
    dal = OrchestrationsDal(db)

    def runner(_goal: str, **kwargs: Any) -> _Result:
        return _Result(run_id=str(kwargs["run_id"]))

    try:
        _create_resumable_run(
            dal,
            auto_card,
            run_id="orch_retry_auto",
            status="failed",
            retry_count=0,
        )
        _create_resumable_run(
            dal,
            capped_card,
            run_id="orch_retry_capped",
            status="failed",
            retry_count=2,
        )
        monkeypatch.setenv("OMNIAGENTOS_ORCH_RETRY_BACKOFF_MINUTES", "0")
        monkeypatch.setattr(service, "_default_orchestrate_runner", runner)

        claimed = service.resume_orphaned_orchestrations(
            store=collab._store,
            collab_store=collab,
        )

        assert claimed == ["orch_retry_auto"]
        auto_row = _wait_for(dal, "orch_retry_auto", "completed")
        assert auto_row["retry_count"] == 1
        capped_row = dal.get("orch_retry_capped")
        assert capped_row is not None
        assert capped_row["status"] == "failed"
        assert capped_row["retry_count"] == 2

        manual = service.resume_orchestration(
            capped_card.id,
            store=collab._store,
            collab_store=collab,
            manual=True,
        )
        assert manual == {
            "run_id": "orch_retry_capped",
            "resumed": True,
            "reason": "manual",
        }
        manual_row = _wait_for(dal, "orch_retry_capped", "completed")
        assert manual_row["retry_count"] == 3
    finally:
        dal.close()


def test_resume_functions_never_touch_longhaul_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "longhaul-resume.db")
    collab = CollabStore(db)
    card = BoardTask(
        title="Longhaul owns this",
        status=BoardTaskStatus.IN_PROGRESS,
        result_ref="orch_longhaul",
    )
    collab.create_board_task(card)
    collab._store._write(  # noqa: SLF001 -- migration-owned lane test fixture.
        "UPDATE board_tasks SET lane = 'longhaul' WHERE id = ?",
        (card.id,),
    )
    dal = OrchestrationsDal(db)
    try:
        _create_resumable_run(dal, card, run_id="orch_longhaul")
        monkeypatch.setattr(
            service,
            "_default_orchestrate_runner",
            lambda *_args, **_kwargs: pytest.fail("longhaul runner must not start"),
        )

        assert (
            service.resume_orphaned_orchestrations(
                store=collab._store,
                collab_store=collab,
            )
            == []
        )
        with pytest.raises(ValueError, match="longhaul"):
            service.resume_orchestration(
                card.id,
                store=collab._store,
                collab_store=collab,
                manual=True,
            )

        row = dal.get("orch_longhaul")
        assert row is not None
        assert row["status"] == "running"
        assert row["resume_count"] == 0
    finally:
        dal.close()


def test_resume_renotifies_pending_step_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "approval-resume.db")
    collab = CollabStore(db)
    card = BoardTask(
        title="Approval resume",
        status=BoardTaskStatus.IN_PROGRESS,
        result_ref="orch_approval_resume",
    )
    collab.create_board_task(card)
    dal = OrchestrationsDal(db)
    notifications: list[dict[str, Any]] = []

    def record_notification(**kwargs: Any) -> str:
        notifications.append(kwargs)
        return "ntf_resume"

    try:
        _create_resumable_run(dal, card, run_id="orch_approval_resume")
        dal.step_session("orch_approval_resume", 1, "ses_pending_approval")
        dal._connection.execute(  # noqa: SLF001 -- pending approval fixture.
            "INSERT INTO approvals "
            "(id, action_class, proposed_action, risk, state, created_at, session_id) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (
                "apr_resume",
                "consequential",
                "publish the result",
                "operator review",
                "2020-01-01T00:00:00Z",
                "ses_pending_approval",
            ),
        )
        monkeypatch.setattr(service, "record_notification", record_notification)
        monkeypatch.setattr(
            service,
            "_default_orchestrate_runner",
            lambda _goal, **_kwargs: _Result(run_id="orch_approval_resume"),
        )

        service.resume_orchestration(
            card.id,
            store=collab._store,
            collab_store=collab,
        )
        _wait_for(dal, "orch_approval_resume", "completed")

        assert len(notifications) == 1
        assert notifications[0]["ref_id"] == "apr_resume"
        assert notifications[0]["payload"]["session_id"] == "ses_pending_approval"
        # Deduped since 2026-07-24: this runs on EVERY resume tick, so dedupe=False
        # stacked a fresh row + desktop banner for one un-clicked approval every few
        # minutes. The reminder still resurfaces once the operator dismisses it —
        # see tests/intake/test_approval_renotify_dedupe.py.
        assert notifications[0]["dedupe"] is True
    finally:
        dal.close()


def test_same_process_live_conductor_refuses_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    db = str(tmp_path / "local-live.db")
    collab = CollabStore(db)
    started = threading.Event()
    release = threading.Event()

    def runner(_goal: str, **kwargs: Any) -> _Result:
        kwargs["checkpoint"].record_plan("orch_local_live", "{}", ["running"])
        started.set()
        assert release.wait(timeout=3)
        return _Result(run_id="orch_local_live")

    result = dispatch_spec(
        collab._store,
        collab,
        load_policy(),
        RefinedSpec(title="Local", description="Keep running"),
        execute="orchestrate",
        async_orchestrate=True,
        orchestration_run_id="orch_local_live",
        orchestrate_runner=runner,
    )
    dal = OrchestrationsDal(db)
    try:
        assert started.wait(timeout=3)
        dal._connection.execute(  # noqa: SLF001 -- misleading stale heartbeat fixture.
            "UPDATE orchestrations SET heartbeat_at = '2020-01-01T00:00:00Z' WHERE id = ?",
            ("orch_local_live",),
        )
        with pytest.raises(ValueError, match="conductor live"):
            service.resume_orchestration(
                str(result["board_task"]["id"]),
                store=collab._store,
                collab_store=collab,
                manual=True,
            )
        assert dal.get("orch_local_live")["resume_count"] == 0  # type: ignore[index]
    finally:
        release.set()
        _wait_for(dal, "orch_local_live", "completed")
        dal.close()


def test_superseded_conductor_terminal_write_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    db = str(tmp_path / "superseded.db")
    collab = CollabStore(db)

    def runner(_goal: str, **kwargs: Any) -> _Result:
        connection = sqlite3.connect(db)
        try:
            connection.execute(
                "UPDATE orchestrations SET conductor_pid = 999001, "
                "conductor_claimed_at = 'takeover' WHERE id = ?",
                (kwargs["run_id"],),
            )
            connection.commit()
        finally:
            connection.close()
        return _Result(run_id=str(kwargs["run_id"]))

    result = dispatch_spec(
        collab._store,
        collab,
        load_policy(),
        RefinedSpec(title="Fence", description="Supersede conductor"),
        execute="orchestrate",
        orchestration_run_id="orch_superseded",
        orchestrate_runner=runner,
    )
    dal = OrchestrationsDal(db)
    try:
        row = dal.get("orch_superseded")
        assert row is not None
        assert row["status"] == "running"
        assert row["conductor_claimed_at"] == "takeover"
        board = collab.get_board_task(str(result["board_task"]["id"]))
        assert board is not None
        assert board["status"] == "in_progress"
    finally:
        dal.close()


class _Spawner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def spawn(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "ses_outputs"


def test_dispatch_creates_output_directories_and_instructs_all_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    spec = RefinedSpec(title="Write it", description="Create the requested file")

    tools = CollabStore(str(tmp_path / "tools.db"))
    tools_result = dispatch_spec(
        tools._store,
        tools,
        load_policy(),
        spec,
        execute="tools",
        harness=HarnessType.MOCK.value,
    )
    tools_dir = Path(tools_result["working_dir"])
    assert (tools_dir / "uploads").is_dir()
    assert (tools_dir / "outputs").is_dir()
    run = tools._store.get_run(str(tools_result["run_id"]))
    assert run is not None
    assert DELIVERABLES in json.loads(run["harness_params"])["prompt"]

    sessions = CollabStore(str(tmp_path / "sessions.db"))
    spawner = _Spawner()
    session_result = dispatch_spec(
        sessions._store,
        sessions,
        load_policy(),
        spec,
        execute="session",
        session_spawner=spawner,
    )
    session_dir = Path(session_result["working_dir"])
    assert (session_dir / "uploads").is_dir()
    assert (session_dir / "outputs").is_dir()
    assert DELIVERABLES in spawner.calls[0]["prompt"]
    assert DELIVERABLES in target_and_todo_prompt("make a file", str(tmp_path))
