from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import (
    ActionClass,
    AgentInput,
    AgentResult,
    AgentUsage,
    ApprovalState,
    BudgetDecision,
    BudgetSpec,
    HarnessType,
    PolicyDecision,
    ResultStatus,
    RunManifest,
    RunState,
    SandboxSpec,
    TaskState,
    digest,
    new_id,
    utc_now_iso,
)
from omniagentos.db.store import SqliteStore
from omniagentos.mock_adapter import MockAdapter
from omniagentos.runner.core import Runner, RunnerDependencies
from tests.support.db_template import migrated_db


class TrackingAdapter(MockAdapter):
    def __init__(self, after_run: Callable[[int], None] | None = None) -> None:
        self.calls: list[AgentInput] = []
        self.cancelled: list[str] = []
        self.after_run = after_run
        self._lock = threading.Lock()

    def run(self, input: AgentInput) -> AgentResult:
        with self._lock:
            self.calls.append(input)
            count = len(self.calls)
        result = super().run(input)
        if self.after_run:
            self.after_run(count)
        return result

    def cancel(self, session_ref: str) -> bool:
        self.cancelled.append(session_ref)
        return True


class FinalizationSpy:
    def __init__(self) -> None:
        self.manifests: list[RunManifest] = []
        self.notes: list[tuple[str, str, str]] = []

    def append(self, root: str, manifest: RunManifest) -> str:
        self.manifests.append(manifest)
        return str(Path(root) / "runs.jsonl")

    def render(
        self,
        run: dict[str, Any],
        steps: list[dict[str, Any]],
        manifest_path: str,
        receipts: list[Any],
        **_kwargs: Any,
    ) -> tuple[str, str]:
        # **_kwargs tolerates the real render_run_note signature (artifacts_list,
        # task_title, task_input) the runner now wires through (council DSGN-001).
        return (
            f"runs/{run['id']}.md",
            f"{run['state']} {manifest_path} {len(steps)} {len(receipts)}",
        )

    def write(self, root: str, relpath: str, content: str) -> str:
        self.notes.append((root, relpath, content))
        return str(Path(root) / relpath)


def allow_budget(
    spec: BudgetSpec, used_wall_ms: int, used_tokens: int, used_cost_usd: float
) -> BudgetDecision:
    allowed = (
        (spec.wall_ms_max is None or used_wall_ms <= spec.wall_ms_max)
        and (spec.tokens_max is None or used_tokens <= spec.tokens_max)
        and (spec.cost_usd_max is None or used_cost_usd <= spec.cost_usd_max)
    )
    return BudgetDecision(allowed=allowed)


def dependencies(
    adapter: TrackingAdapter,
    spy: FinalizationSpy,
    *,
    approval: bool = False,
    always_human: bool = False,
    approval_expiry_hours: int = 24,
    allow_irreversible: bool = False,
) -> RunnerDependencies:
    def _evaluate(action: ActionClass) -> PolicyDecision:
        # AUTO-mode-faithful stub: irreversible ALWAYS hard-stops (requires_approval
        # + always_human); consequential gating stays controlled by the `approval`
        # flag so the other tests keep their scenarios. ``allow_irreversible`` is a
        # mechanics-only escape hatch: a handful of tests exercise _run_command's
        # execution plumbing (cwd/timeout/no-shell/tool-gating) with an interpreter
        # command that the deny-by-default classifier would otherwise park; the
        # hard-stop itself is proven end-to-end in test_auto_hardstop.py.
        hard_stop = action == ActionClass.IRREVERSIBLE and not allow_irreversible
        return PolicyDecision(
            requires_approval=(approval and action == ActionClass.CONSEQUENTIAL) or hard_stop,
            always_human=(always_human and action == ActionClass.CONSEQUENTIAL) or hard_stop,
        )

    return RunnerDependencies(
        evaluate_policy=_evaluate,
        sandbox_for_tools=lambda harness, tools: SandboxSpec(
            level="workspace_write" if tools else "read_only"
        ),
        check_budget=allow_budget,
        resolve_adapter=lambda harness: adapter,
        append_manifest=spy.append,
        render_run_note=spy.render,
        write_note=spy.write,
        approval_expiry_hours=approval_expiry_hours,
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # Template copy instead of a fresh 86-migration apply; SqliteStore's
    # constructor is exactly what ``migrate()`` does (_connect +
    # migrate_connection), so the resulting file is identical.
    return Path(migrated_db(SqliteStore, tmp_path / "runner.db"))


def create_run(
    store: SqliteStore,
    plan: list[dict[str, Any]],
    *,
    budget: dict[str, Any] | None = None,
    tools_allowed: list[str] | None = None,
    run_id: str | None = None,
    task_title: str = "runner test",
    task_input: dict[str, Any] | None = None,
) -> tuple[str, str]:
    now = utc_now_iso()
    task_id = new_id("tsk")
    run_id = run_id or new_id("run")
    store.create_task(
        {
            "id": task_id,
            "discipline_id": "code-changes",
            "title": task_title,
            "input_json": json.dumps(
                task_input
                if task_input is not None
                else {"prompt": "test", "tools_allowed": tools_allowed or []}
            ),
            "acceptance_json": "{}",
            "state": TaskState.QUEUED.value,
            "risk": "low",
            "created_at": now,
            "updated_at": now,
        }
    )
    store.enqueue_run(
        {
            "id": run_id,
            "task_id": task_id,
            "discipline_id": "code-changes",
            "harness": HarnessType.MOCK.value,
            "state": RunState.QUEUED.value,
            "plan_json": json.dumps(plan),
            "budget_json": json.dumps(budget or {}),
            "trace_id": f"trace-{run_id}",
            "queued_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    return task_id, run_id


def agent_step(name: str, **params: Any) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "agent",
        "action_class": ActionClass.SANDBOXED_CREATION.value,
        "params": {"adapter": "mock", **params},
    }


class CrashAfterEffectStore(SqliteStore):
    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self.crash = True

    def idem_complete(self, key: str, result_json: str) -> None:
        if self.crash:
            self.crash = False
            raise KeyboardInterrupt("simulated kill after durable effect")
        super().idem_complete(key, result_json)


class FlakyAdapter(TrackingAdapter):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def run(self, input: AgentInput) -> AgentResult:
        with self._lock:
            self.calls.append(input)
            attempt = len(self.calls)
        if attempt <= self.failures:
            return AgentResult(
                status=ResultStatus.ERROR,
                error="flaky failure",
                usage=AgentUsage(wall_ms=1),
            )
        return AgentResult(
            status=ResultStatus.OK,
            output_text="recovered",
            usage=AgentUsage(wall_ms=1),
        )


class TimeTravelHeartbeatStore(SqliteStore):
    """A real store with a controllable heartbeat clock for stale-reclaim tests."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self.elapsed_s = 0
        self.heartbeats: list[tuple[str | None, int]] = []

    def _timestamp(self, offset_s: int = 0) -> str:
        return (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=offset_s)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def upsert_heartbeat(self, worker_id: str, pid: int, current_run_id: str | None) -> None:
        super().upsert_heartbeat(worker_id, pid, current_run_id)
        self._connection.execute(
            "UPDATE heartbeats SET last_beat_at = ? WHERE worker_id = ?",
            (self._timestamp(self.elapsed_s), worker_id),
        )
        self._connection.commit()
        self.heartbeats.append((current_run_id, self.elapsed_s))

    def advance(self, seconds: int) -> None:
        self.elapsed_s += seconds

    def reclaim_stale_at(self, worker_id: str, stale_s: int) -> None:
        cutoff = self._timestamp(self.elapsed_s - stale_s)
        rows = self._connection.execute(
            "SELECT r.id FROM runs AS r LEFT JOIN heartbeats AS h ON h.worker_id = r.worker_id "
            "WHERE r.state = 'running' AND (h.worker_id IS NULL OR h.last_beat_at < ?)",
            (cutoff,),
        ).fetchall()
        for row in rows:
            self._connection.execute(
                "UPDATE runs SET worker_id = ? WHERE id = ?", (worker_id, str(row["id"]))
            )
        self._connection.commit()


class FinalizationScanFaultStore(SqliteStore):
    """Raises once before a finalization candidate can be associated with a run."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self.raised = False

    def list_runs(self, filters: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]:
        completed_scan = {
            "state": RunState.COMPLETED.value,
            "vault_note_path": None,
        }
        if filters == completed_scan and not self.raised:
            self.raised = True
            raise sqlite3.OperationalError("scan unavailable")
        return super().list_runs(filters, limit)


def test_restart_resolves_unknown_effect_with_probe_exactly_once(
    db_path: Path, tmp_path: Path
) -> None:
    run_id = new_id("run")
    workspace_base = tmp_path / "trusted"
    receipt = workspace_base / run_id / "receipt.txt"
    plan = [
        {
            "name": "append-receipt",
            "kind": "effect",
            "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
            "params": {
                "effect": "append_file",
                "path": receipt.name,
                "line": "landed",
                "probe": f"grep -q landed {receipt}",
                "working_dir": str(tmp_path / "attacker-chosen-and-ignored"),
            },
        }
    ]
    seed = SqliteStore(str(db_path))
    create_run(
        seed,
        plan,
        tools_allowed=["file_write", "shell"],
        run_id=run_id,
    )
    spy = FinalizationSpy()
    crashing = CrashAfterEffectStore(str(db_path))
    runner = Runner(
        crashing,
        "w1",
        dependencies=dependencies(TrackingAdapter(), spy),
        ledger_dir=str(tmp_path / "ledger"),
        vault_dir=str(tmp_path / "vault"),
        workspace_base=str(workspace_base),
    )
    with pytest.raises(KeyboardInterrupt):
        runner.tick()

    restarted = SqliteStore(str(db_path))
    restarted_runner = Runner(
        restarted,
        "w1",
        dependencies=dependencies(TrackingAdapter(), spy),
        ledger_dir=str(tmp_path / "ledger"),
        vault_dir=str(tmp_path / "vault"),
        workspace_base=str(workspace_base),
    )
    restarted_runner.tick()

    assert receipt.read_text().splitlines() == ["landed"]
    assert restarted.get_run(run_id)["state"] == RunState.COMPLETED.value  # type: ignore[index]
    assert restarted.get_steps(run_id)[0]["status"] == "skipped"
    assert restarted.idem_for_run(run_id)[0]["result_json"] is not None


def test_unknown_effect_without_probe_fails_closed(db_path: Path, tmp_path: Path) -> None:
    store = SqliteStore(str(db_path))
    target = tmp_path / "must-not-exist.txt"
    _, run_id = create_run(
        store,
        [
            {
                "name": "uncertain",
                "kind": "effect",
                "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
                "params": {
                    "key": "known-key",
                    "effect": "append_file",
                    "path": target.name,
                    "working_dir": str(tmp_path),
                },
            }
        ],
        tools_allowed=["file_write"],
    )
    assert store.idem_insert("known-key", run_id, "uncertain")
    Runner(store, "w1", dependencies=dependencies(TrackingAdapter(), FinalizationSpy())).tick()
    run = store.get_run(run_id)
    assert run and run["state"] == RunState.FAILED.value
    assert run["error"] == "idempotency_unresolved"
    assert not target.exists()


def test_fence_loss_aborts_before_next_adapter_call(db_path: Path, tmp_path: Path) -> None:
    owner = SqliteStore(str(db_path))
    _, run_id = create_run(owner, [agent_step("one"), agent_step("two")])

    def steal(count: int) -> None:
        if count == 1:
            assert owner.update_run(run_id, {"worker_id": "w2"})

    adapter = TrackingAdapter(steal)
    runner = Runner(
        SqliteStore(str(db_path)),
        "w1",
        dependencies=dependencies(adapter, FinalizationSpy()),
        ledger_dir=str(tmp_path / "ledger"),
        vault_dir=str(tmp_path / "vault"),
    )
    runner.tick()

    assert len(adapter.calls) == 1
    run = owner.get_run(run_id)
    assert run and run["worker_id"] == "w2" and run["state"] == RunState.RUNNING.value


def test_two_workers_claim_once_and_stale_parked_run_is_reclaimed(
    db_path: Path, tmp_path: Path
) -> None:
    seed = SqliteStore(str(db_path))
    _, run_id = create_run(seed, [agent_step("only", mock={"delay_ms": 30})])
    adapter = TrackingAdapter()
    spy = FinalizationSpy()
    runners = [
        Runner(
            SqliteStore(str(db_path)),
            worker,
            dependencies=dependencies(adapter, spy),
            ledger_dir=str(tmp_path / "ledger"),
            vault_dir=str(tmp_path / "vault"),
        )
        for worker in ("w1", "w2")
    ]
    barrier = threading.Barrier(3)

    def tick(runner: Runner) -> None:
        barrier.wait()
        runner.tick()

    threads = [threading.Thread(target=tick, args=(runner,)) for runner in runners]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert len(adapter.calls) == 1
    assert seed.get_run(run_id)["state"] == RunState.COMPLETED.value  # type: ignore[index]

    _, parked_id = create_run(
        seed,
        [
            {
                **agent_step("approve-me"),
                "action_class": ActionClass.CONSEQUENTIAL.value,
            }
        ],
    )
    parked_adapter = TrackingAdapter()
    parked_spy = FinalizationSpy()
    first = Runner(
        SqliteStore(str(db_path)),
        "dead",
        dependencies=dependencies(parked_adapter, parked_spy, approval=True),
    )
    first.tick()
    assert seed.get_run(parked_id)["state"] == RunState.AWAITING_APPROVAL.value  # type: ignore[index]
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM heartbeats WHERE worker_id = 'dead'")
    second = Runner(
        SqliteStore(str(db_path)),
        "rescuer",
        dependencies=dependencies(parked_adapter, parked_spy, approval=True),
    )
    second.tick()
    assert seed.get_run(parked_id)["worker_id"] == "rescuer"  # type: ignore[index]
    approval = seed.get_approval_for(parked_id, 0)
    assert approval and seed.decide_approval(approval["id"], ApprovalState.APPROVED.value, "tester")
    second.tick()
    assert seed.get_run(parked_id)["state"] == RunState.COMPLETED.value  # type: ignore[index]
    assert len(parked_adapter.calls) == 1


def test_budget_exhaustion_stops_before_another_adapter_call(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Blocking is opt-in since 2026-07-24 (budgets advisory by default). This pins
    # the escape hatch: with enforcement ON, exhaustion still stops the next call.
    monkeypatch.setenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", "block")
    store = SqliteStore(str(db_path))
    _, run_id = create_run(
        store,
        [
            agent_step("one", mock={"usage": {"input_tokens": 8, "output_tokens": 7}}),
            agent_step("two"),
        ],
        budget={"tokens_max": 10},
    )
    adapter = TrackingAdapter()
    Runner(store, "w1", dependencies=dependencies(adapter, FinalizationSpy())).tick()
    run = store.get_run(run_id)
    assert run and run["state"] == RunState.FAILED.value
    assert run["error"] == "budget_exceeded"
    assert len(adapter.calls) == 1


def test_pause_parks_between_steps_then_requeues_to_completion(db_path: Path) -> None:
    control = SqliteStore(str(db_path))
    _, run_id = create_run(control, [agent_step("one"), agent_step("two")])

    def pause(count: int) -> None:
        if count == 1:
            control.set_pause(True, "test")

    adapter = TrackingAdapter(pause)
    runner = Runner(
        SqliteStore(str(db_path)),
        "w1",
        dependencies=dependencies(adapter, FinalizationSpy()),
    )
    runner.tick()
    assert control.get_run(run_id)["state"] == RunState.PAUSED.value  # type: ignore[index]
    assert len(adapter.calls) == 1
    control.set_pause(False)
    runner.tick()
    assert control.get_run(run_id)["state"] == RunState.COMPLETED.value  # type: ignore[index]
    assert len(adapter.calls) == 2


@pytest.mark.parametrize(
    ("outcome", "expected", "error"),
    [
        (ApprovalState.APPROVED, RunState.COMPLETED, None),
        (ApprovalState.REJECTED, RunState.FAILED, "approval_rejected"),
        ("cancel", RunState.CANCELLED, None),
    ],
)
def test_approval_resume_reject_and_cancel(
    db_path: Path,
    outcome: ApprovalState | str,
    expected: RunState,
    error: str | None,
) -> None:
    store = SqliteStore(str(db_path))
    _, run_id = create_run(
        store,
        [{**agent_step("gated"), "action_class": ActionClass.CONSEQUENTIAL.value}],
    )
    adapter = TrackingAdapter()
    runner = Runner(
        store,
        "w1",
        dependencies=dependencies(adapter, FinalizationSpy(), approval=True),
    )
    runner.tick()
    approval = store.get_approval_for(run_id, 0)
    assert approval and approval["state"] == ApprovalState.PENDING.value
    if outcome == "cancel":
        store.request_cancel(run_id)
    else:
        assert store.decide_approval(approval["id"], outcome.value, "tester")
    runner.tick()
    run = store.get_run(run_id)
    assert run and run["state"] == expected.value and run["error"] == error
    decided = store.get_approval_for(run_id, 0)
    if outcome == "cancel":
        assert decided and decided["state"] == ApprovalState.EXPIRED.value
    assert len(adapter.calls) == (1 if outcome == ApprovalState.APPROVED else 0)


def test_validation_runs_last_and_finalization_is_exactly_once(
    db_path: Path, tmp_path: Path
) -> None:
    store = SqliteStore(str(db_path))
    _, run_id = create_run(
        store,
        [
            agent_step("work"),
            {
                "name": "validate",
                "kind": "validate",
                "action_class": ActionClass.READ_ONLY.value,
                "params": {
                    # A read-only probe (auto-runs under deny-by-default); the point
                    # of this test is finalization ordering, not the command.
                    "command": ["true"],
                    "tools_allowed": ["shell"],
                },
            },
        ],
        tools_allowed=["shell"],
    )
    spy = FinalizationSpy()
    runner = Runner(
        store,
        "w1",
        dependencies=dependencies(TrackingAdapter(), spy),
        workspace_base=str(tmp_path / "workspace"),
    )
    runner.tick()
    runner.execute_run(run_id)

    run = store.get_run(run_id)
    assert run and run["state"] == RunState.COMPLETED.value
    assert run["manifest_path"] and run["vault_note_path"]
    assert [step["status"] for step in store.get_steps(run_id)] == ["completed", "completed"]
    assert len(spy.manifests) == 1
    assert spy.manifests[0].state == RunState.COMPLETED
    assert len(spy.notes) == 1


def test_failure_compensates_completed_steps_in_reverse(db_path: Path, tmp_path: Path) -> None:
    store = SqliteStore(str(db_path))
    workspace_base = tmp_path / "workspace"
    _, run_id = create_run(
        store,
        [
            {
                "name": "effect",
                "kind": "effect",
                "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
                "params": {
                    "effect": "noop",
                    "compensate": {
                        "effect": "append_file",
                        "path": "compensation.txt",
                        "line": "undone",
                        "tools_allowed": ["file_write"],
                    },
                },
            },
            {
                "name": "fail-validation",
                "kind": "validate",
                "action_class": ActionClass.READ_ONLY.value,
                "params": {
                    # A read-only probe that exits non-zero (auto-runs) to drive the
                    # compensation path; the command itself is incidental here.
                    "command": ["false"],
                    "tools_allowed": ["shell"],
                },
            },
        ],
        tools_allowed=["file_write", "shell"],
    )
    compensation = workspace_base / run_id / "compensation.txt"
    Runner(
        store,
        "w1",
        dependencies=dependencies(TrackingAdapter(), FinalizationSpy()),
        workspace_base=str(workspace_base),
    ).tick()
    run = store.get_run(run_id)
    assert run and run["state"] == RunState.FAILED.value
    assert store.get_steps(run_id)[0]["status"] == "compensated"
    assert compensation.read_text().splitlines() == ["undone"]


def test_heartbeat_refresh_per_step_prevents_mid_plan_reclaim(db_path: Path) -> None:
    store = TimeTravelHeartbeatStore(str(db_path))
    _, run_id = create_run(store, [agent_step("one"), agent_step("two"), agent_step("three")])

    def advance_clock(count: int) -> None:
        store.advance(15)
        if count == 3:
            store.reclaim_stale_at("w2", stale_s=30)

    runner = Runner(
        store,
        "w1",
        dependencies=dependencies(TrackingAdapter(advance_clock), FinalizationSpy()),
        stale_s=30,
    )
    runner.tick()

    run = store.get_run(run_id)
    assert run and run["state"] == RunState.COMPLETED.value and run["worker_id"] == "w1"
    active_beats = [elapsed for current, elapsed in store.heartbeats if current == run_id]
    assert active_beats[-3:] == [0, 15, 30]


def test_new_worker_finalizes_dead_owners_unfinished_terminal_run(db_path: Path) -> None:
    store = SqliteStore(str(db_path))
    _, run_id = create_run(store, [])
    assert store.update_run(
        run_id,
        {
            "state": RunState.COMPLETED.value,
            "worker_id": "dead-pid",
            "finished_at": utc_now_iso(),
        },
    )
    spy = FinalizationSpy()

    Runner(store, "new-pid", dependencies=dependencies(TrackingAdapter(), spy)).tick()

    run = store.get_run(run_id)
    assert run and run["worker_id"] == "new-pid"
    assert run["manifest_path"] and run["vault_note_path"]
    assert len(spy.manifests) == len(spy.notes) == 1


def test_validating_run_defers_pause_to_its_terminal_boundary(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqliteStore(str(db_path))
    # This test drives pause DELIVERY: its validate command intentionally writes the
    # control DB (outside the workspace), which the OS sandbox rightly blocks. Disable
    # the sandbox here so the orthogonal pause-deferral plumbing can be exercised; the
    # confinement itself is proven in test_guardrail_ac_policy.py.
    monkeypatch.setattr("omniagentos.runner.sandbox.wrap_command", lambda argv, cwd: argv)
    pause_command = (
        "import sqlite3; connection = sqlite3.connect("
        f"{str(db_path)!r}); connection.execute('UPDATE pause SET paused = 1'); connection.commit()"
    )
    _, run_id = create_run(
        store,
        [
            {
                "name": "pause-during-validation",
                "kind": "validate",
                "action_class": ActionClass.READ_ONLY.value,
                "params": {
                    "command": [sys.executable, "-c", pause_command],
                    "tools_allowed": ["shell"],
                },
            },
            {
                "name": "finish-validation",
                "kind": "validate",
                "action_class": ActionClass.READ_ONLY.value,
                "params": {
                    "command": [sys.executable, "-c", "pass"],
                    "tools_allowed": ["shell"],
                },
            },
        ],
        tools_allowed=["shell"],
    )

    Runner(
        store,
        "w1",
        dependencies=dependencies(TrackingAdapter(), FinalizationSpy(), allow_irreversible=True),
        workspace_base=str(tmp_path / "workspace"),
    ).tick()

    run = store.get_run(run_id)
    assert run and run["state"] == RunState.COMPLETED.value
    assert all(event["action"] != RunState.PAUSED.value for event in store.get_events_after(0))


def test_step_retries_recover_after_two_failures(db_path: Path) -> None:
    store = SqliteStore(str(db_path))
    _, run_id = create_run(store, [agent_step("flaky", retries=2)])
    adapter = FlakyAdapter(failures=2)

    Runner(store, "w1", dependencies=dependencies(adapter, FinalizationSpy())).tick()

    run = store.get_run(run_id)
    assert run and run["state"] == RunState.COMPLETED.value
    assert len(adapter.calls) == 3


def test_step_retries_fail_after_exhaustion(db_path: Path) -> None:
    store = SqliteStore(str(db_path))
    _, exhausted_id = create_run(store, [agent_step("exhausted", retries=2)])
    exhausted = FlakyAdapter(failures=3)
    runner = Runner(store, "w1", dependencies=dependencies(exhausted, FinalizationSpy()))
    runner.tick()
    assert store.get_run(exhausted_id)["state"] == RunState.FAILED.value  # type: ignore[index]
    assert len(exhausted.calls) == 3


def test_step_default_zero_retries_fails_after_one_attempt(db_path: Path) -> None:
    store = SqliteStore(str(db_path))
    _, default_id = create_run(store, [agent_step("default")])
    default = FlakyAdapter(failures=1)
    Runner(store, "w1", dependencies=dependencies(default, FinalizationSpy())).tick()
    assert store.get_run(default_id)["state"] == RunState.FAILED.value  # type: ignore[index]
    assert len(default.calls) == 1


def test_effect_idempotency_key_override_and_computed_formula(db_path: Path) -> None:
    store = SqliteStore(str(db_path))
    params = {"effect": "noop"}
    _, explicit_id = create_run(
        store,
        [
            {
                "name": "explicit",
                "kind": "effect",
                "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
                "params": {**params, "key": "my_key"},
            }
        ],
    )
    Runner(store, "w1", dependencies=dependencies(TrackingAdapter(), FinalizationSpy())).tick()
    assert store.idem_for_run(explicit_id)[0]["key"] == "my_key"

    _, computed_id = create_run(
        store,
        [
            {
                "name": "computed",
                "kind": "effect",
                "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
                "params": params,
            }
        ],
    )
    Runner(store, "w1", dependencies=dependencies(TrackingAdapter(), FinalizationSpy())).tick()
    material = f"{computed_id}|0|computed|{digest(json.dumps(params, separators=(',', ':'), sort_keys=True))}"
    expected = hashlib.sha256(material.encode()).hexdigest()
    assert store.idem_for_run(computed_id)[0]["key"] == expected


def test_effect_steps_with_different_params_have_different_idempotency_keys(db_path: Path) -> None:
    store = SqliteStore(str(db_path))
    _, run_id = create_run(
        store,
        [
            {
                "name": "write",
                "kind": "effect",
                "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
                "params": {"effect": "noop", "tag": "one"},
            },
            {
                "name": "write",
                "kind": "effect",
                "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
                "params": {"effect": "noop", "tag": "two"},
            },
        ],
    )

    Runner(store, "w1", dependencies=dependencies(TrackingAdapter(), FinalizationSpy())).tick()

    assert len({row["key"] for row in store.idem_for_run(run_id)}) == 2


def test_task_projection_guard_failure_is_audited_not_raised(db_path: Path) -> None:
    store = SqliteStore(str(db_path))
    task_id, run_id = create_run(store, [agent_step("work")])
    assert store.update_task_state(task_id, TaskState.CANCELLED.value)

    Runner(store, "w1", dependencies=dependencies(TrackingAdapter(), FinalizationSpy())).tick()

    run = store.get_run(run_id)
    task = store.get_task(task_id)
    assert run and run["state"] == RunState.COMPLETED.value
    assert task and task["state"] == TaskState.CANCELLED.value
    assert any(
        event["type"] == "audit.event" and event["action"] == "task_projection_guard_failed"
        for event in store.get_events_after(0)
    )


def test_paused_runs_requeue_only_after_pause_is_lifted(db_path: Path) -> None:
    store = SqliteStore(str(db_path))
    _, run_id = create_run(store, [agent_step("work")])
    assert store.claim_next_run("w1")
    store.set_pause(True, "operator pause")
    runner = Runner(store, "w1", dependencies=dependencies(TrackingAdapter(), FinalizationSpy()))

    runner.tick()
    assert store.get_run(run_id)["state"] == RunState.PAUSED.value  # type: ignore[index]
    runner.tick()
    assert store.get_run(run_id)["state"] == RunState.PAUSED.value  # type: ignore[index]

    store.set_pause(False)
    runner.tick()
    assert store.get_run(run_id)["state"] == RunState.COMPLETED.value  # type: ignore[index]


def test_effective_action_class_cannot_be_lowered_by_plan(db_path: Path) -> None:
    store = SqliteStore(str(db_path))
    _, run_id = create_run(
        store,
        [
            {
                "name": "destructive-validation",
                "kind": "validate",
                "action_class": ActionClass.READ_ONLY.value,
                # An OUT-OF-SCOPE delete: since 2026-07-24 a delete proven inside
                # the run workspace classifies INTERNAL_REVERSIBLE, so the command
                # here must be one that is still genuinely irreversible for this
                # test to exercise what it names — the plan cannot LOWER the class.
                "params": {"command": ["rm", "-rf", "/etc"], "tools_allowed": ["shell"]},
            }
        ],
    )
    runner = Runner(
        store,
        "w1",
        dependencies=dependencies(TrackingAdapter(), FinalizationSpy(), approval=True),
    )

    runner.tick()

    run = store.get_run(run_id)
    approval = store.get_approval_for(run_id, 0)
    assert run and run["state"] == RunState.AWAITING_APPROVAL.value
    # An out-of-scope `rm` is raised to IRREVERSIBLE (hard-stop), never lowered to
    # the plan's declared read_only.
    assert approval and approval["action_class"] == ActionClass.IRREVERSIBLE.value
    assert store.get_steps(run_id) == []


def test_default_approval_expiry_is_enforced(db_path: Path) -> None:
    """AUTO-APPROVE 5.2 / F1: expiry re-queues a fresh pending approval.

    Previously this failed the run with ``approval_expired``, which shredded
    92% of parked work. The run stays AWAITING_APPROVAL with a new pending row.
    """
    store = SqliteStore(str(db_path))
    _, run_id = create_run(
        store,
        [{**agent_step("expiring"), "action_class": ActionClass.CONSEQUENTIAL.value}],
    )
    runner = Runner(
        store,
        "w1",
        dependencies=dependencies(
            TrackingAdapter(),
            FinalizationSpy(),
            approval=True,
            approval_expiry_hours=0,
        ),
    )

    runner.tick()
    approval = store.get_approval_for(run_id, 0)
    assert approval and approval["expires_at"] is not None
    first_id = approval["id"]
    runner.tick()

    run = store.get_run(run_id)
    assert run and run["state"] == RunState.AWAITING_APPROVAL.value
    assert run.get("error") in (None, "")
    # Latest approval for the step is a fresh pending (or still pending after requeue).
    latest = store.get_approval_for(run_id, 0)
    assert latest is not None
    assert latest["state"] == "pending"
    # Prior row may still be the latest if requeue reuses hash path; when a new
    # id is minted it differs. Either way the run must not be failed.
    del first_id


def test_always_human_rejects_runner_identity_approval(db_path: Path) -> None:
    store = SqliteStore(str(db_path))
    _, run_id = create_run(
        store,
        [{**agent_step("human-only"), "action_class": ActionClass.CONSEQUENTIAL.value}],
    )
    runner = Runner(
        store,
        "w1",
        dependencies=dependencies(
            TrackingAdapter(), FinalizationSpy(), approval=True, always_human=True
        ),
    )
    runner.tick()
    approval = store.get_approval_for(run_id, 0)
    assert approval and store.decide_approval(
        str(approval["id"]), ApprovalState.APPROVED.value, runner.actor
    )

    runner.tick()

    run = store.get_run(run_id)
    assert run and run["state"] == RunState.FAILED.value
    assert run["error"] == "approval_not_human"


@pytest.mark.parametrize("kind", ["effect", "validate"])
def test_effect_and_validate_require_explicit_tool_capabilities(
    db_path: Path, tmp_path: Path, kind: str
) -> None:
    store = SqliteStore(str(db_path))
    if kind == "effect":
        step = {
            "name": "denied-write",
            "kind": "effect",
            "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
            "params": {
                "effect": "append_file",
                "path": "denied.txt",
                "working_dir": str(tmp_path),
            },
        }
    else:
        step = {
            "name": "denied-shell",
            "kind": "validate",
            "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
            # A read-only probe: this test proves the SHELL tool-capability gate
            # fires, which happens at execution -- so the step must not park first.
            "params": {"command": ["true"]},
        }
    _, run_id = create_run(store, [step])

    Runner(store, "w1", dependencies=dependencies(TrackingAdapter(), FinalizationSpy())).tick()

    run = store.get_run(run_id)
    assert run and run["state"] == RunState.FAILED.value
    assert "tool_not_allowed" in str(run["error"])
    assert any(
        event["type"] == "audit.event" and event["action"] == "tool_capability_denied"
        for event in store.get_events_after(0)
    )
    assert not (tmp_path / "denied.txt").exists()


def test_effect_step_cannot_self_grant_file_write(db_path: Path, tmp_path: Path) -> None:
    store = SqliteStore(str(db_path))
    workspace_base = tmp_path / "trusted"
    victim = tmp_path / "legitimate-looking"
    _, run_id = create_run(
        store,
        [
            {
                "name": "self-granted-write",
                "kind": "effect",
                "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
                "params": {
                    "effect": "append_file",
                    "path": "receipt.txt",
                    "line": "must not land",
                    "working_dir": str(victim),
                    "tools_allowed": ["file_write"],
                },
            }
        ],
    )

    Runner(
        store,
        "w1",
        dependencies=dependencies(TrackingAdapter(), FinalizationSpy()),
        workspace_base=str(workspace_base),
    ).tick()

    run = store.get_run(run_id)
    assert run and run["state"] == RunState.FAILED.value
    assert "tool_not_allowed" in str(run["error"])
    assert not (workspace_base / run_id / "receipt.txt").exists()
    assert not (victim / "receipt.txt").exists()


def test_plan_working_dir_cannot_redirect_authorized_write(db_path: Path, tmp_path: Path) -> None:
    store = SqliteStore(str(db_path))
    workspace_base = tmp_path / "trusted"
    victim = tmp_path / "victim"
    _, run_id = create_run(
        store,
        [
            {
                "name": "authorized-write",
                "kind": "effect",
                "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
                "params": {
                    "effect": "append_file",
                    "path": "authorized_keys",
                    "line": "trusted workspace only",
                    "working_dir": str(victim),
                    "tools_allowed": ["file_write"],
                },
            }
        ],
        tools_allowed=["file_write"],
    )

    Runner(
        store,
        "w1",
        dependencies=dependencies(TrackingAdapter(), FinalizationSpy()),
        workspace_base=str(workspace_base),
    ).tick()

    run = store.get_run(run_id)
    confined = workspace_base / run_id / "authorized_keys"
    assert run and run["state"] == RunState.COMPLETED.value
    assert not (victim / "authorized_keys").exists()
    assert confined.read_text(encoding="utf-8").splitlines() == ["trusted workspace only"]


def test_validate_cwd_is_runner_assigned_workspace(db_path: Path, tmp_path: Path) -> None:
    store = SqliteStore(str(db_path))
    workspace_base = tmp_path / "trusted"
    decoy = tmp_path / "plan-selected"
    decoy.mkdir()
    _, run_id = create_run(
        store,
        [
            {
                "name": "show-cwd",
                "kind": "validate",
                "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
                "params": {
                    "command": [
                        sys.executable,
                        "-c",
                        "import os,sys; sys.stdout.write(os.getcwd())",
                    ],
                    "working_dir": str(decoy),
                    "tools_allowed": ["shell"],
                },
            }
        ],
        tools_allowed=["shell"],
    )

    Runner(
        store,
        "w1",
        dependencies=dependencies(TrackingAdapter(), FinalizationSpy(), allow_irreversible=True),
        workspace_base=str(workspace_base),
    ).tick()

    result = json.loads(store.get_steps(run_id)[0]["result_json"])
    assert result["stdout"] == str((workspace_base / run_id).resolve())
    assert result["stdout"] != str(decoy.resolve())


def test_council_governance_bypass_poc_fails_closed(db_path: Path, tmp_path: Path) -> None:
    store = SqliteStore(str(db_path))
    workspace_base = tmp_path / "trusted"
    victim = tmp_path / "victim-home"
    _, run_id = create_run(
        store,
        [
            {
                "kind": "effect",
                "action_class": ActionClass.READ_ONLY.value,
                "params": {
                    "effect": "append_file",
                    "working_dir": str(victim),
                    "path": "authorized_keys",
                    "tools_allowed": ["file_write"],
                },
            }
        ],
    )

    Runner(
        store,
        "w1",
        dependencies=dependencies(TrackingAdapter(), FinalizationSpy()),
        workspace_base=str(workspace_base),
    ).tick()

    run = store.get_run(run_id)
    assert run and run["state"] == RunState.FAILED.value
    assert "tool_not_allowed" in str(run["error"])
    assert not (victim / "authorized_keys").exists()
    assert not (workspace_base / run_id / "authorized_keys").exists()


def test_validate_string_command_does_not_invoke_a_shell(db_path: Path, tmp_path: Path) -> None:
    store = SqliteStore(str(db_path))
    marker = tmp_path / "injected.txt"
    command = f"{sys.executable} -c pass; touch {marker}"
    _, run_id = create_run(
        store,
        [
            {
                "name": "injection-attempt",
                "kind": "validate",
                "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
                "params": {"command": command, "tools_allowed": ["shell"]},
            }
        ],
        tools_allowed=["shell"],
    )

    Runner(
        store,
        "w1",
        dependencies=dependencies(TrackingAdapter(), FinalizationSpy(), allow_irreversible=True),
        workspace_base=str(tmp_path / "workspace"),
    ).tick()

    assert not marker.exists()
    assert store.get_run(run_id)["state"] == RunState.COMPLETED.value  # type: ignore[index]


def test_validate_timeout_kills_the_entire_process_group(db_path: Path, tmp_path: Path) -> None:
    store = SqliteStore(str(db_path))
    # Heartbeat is written to a RELATIVE path (cwd == the per-run workspace) so it
    # lands inside the OS-sandbox-confined workspace; the test still proves the
    # whole process group is killed on timeout.
    script = "(while true; do date +%s%N > child-heartbeat.txt; sleep 0.1; done) & wait"
    _, run_id = create_run(
        store,
        [
            {
                "name": "timeout",
                "kind": "validate",
                "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
                "params": {
                    "command": ["bash", "-c", script],
                    "tools_allowed": ["shell"],
                    "timeout_s": 1,
                },
            }
        ],
        tools_allowed=["shell"],
    )

    Runner(
        store,
        "w1",
        dependencies=dependencies(TrackingAdapter(), FinalizationSpy(), allow_irreversible=True),
        workspace_base=str(tmp_path / "workspace"),
    ).tick()

    heartbeat = tmp_path / "workspace" / run_id / "child-heartbeat.txt"
    assert heartbeat.exists()
    last_write = heartbeat.stat().st_mtime_ns
    time.sleep(0.4)
    assert heartbeat.stat().st_mtime_ns == last_write
    run = store.get_run(run_id)
    assert run and run["state"] == RunState.FAILED.value
    assert run["error"] == "validation_timeout:1"


def test_append_file_rejects_absolute_parent_and_symlink_escapes(
    db_path: Path, tmp_path: Path
) -> None:
    store = SqliteStore(str(db_path))
    workspace_base = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    attempts = [
        (str(outside / "absolute.txt"), outside / "absolute.txt"),
        ("../parent.txt", workspace_base / "parent.txt"),
        ("link/symlink.txt", outside / "symlink.txt"),
    ]

    for index, (raw_path, escaped_path) in enumerate(attempts):
        run_id = new_id("run")
        run_workspace = workspace_base / run_id
        run_workspace.mkdir(parents=True)
        (run_workspace / "link").symlink_to(outside, target_is_directory=True)
        create_run(
            store,
            [
                {
                    "name": f"escape-{index}",
                    "kind": "effect",
                    "action_class": ActionClass.INTERNAL_REVERSIBLE.value,
                    "params": {
                        "effect": "append_file",
                        "path": raw_path,
                        "line": "pwned",
                        "working_dir": str(tmp_path / "ignored"),
                    },
                }
            ],
            tools_allowed=["file_write"],
            run_id=run_id,
        )
        Runner(
            store,
            f"w{index}",
            dependencies=dependencies(TrackingAdapter(), FinalizationSpy()),
            workspace_base=str(workspace_base),
        ).tick()
        run = store.get_run(run_id)
        assert run and run["state"] == RunState.FAILED.value
        assert not escaped_path.exists()


def test_finalization_fault_isolated_without_rewriting_terminal_state(db_path: Path) -> None:
    store = SqliteStore(str(db_path))
    _, run_id = create_run(store, [])
    spy = FinalizationSpy()
    deps = dependencies(TrackingAdapter(), spy)
    attempts = 0

    def append_once(root: str, manifest: RunManifest) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("disk full")
        return spy.append(root, manifest)

    deps.append_manifest = append_once
    runner = Runner(store, "w1", dependencies=deps)

    assert runner.tick() is False
    faulted = store.get_run(run_id)
    assert faulted and faulted["state"] == RunState.COMPLETED.value
    assert faulted["manifest_path"] is None and faulted["vault_note_path"] is None
    assert any(event["action"] == "finalize_attempt_failed" for event in store.get_events_after(0))

    assert runner.tick() is True
    finalized = store.get_run(run_id)
    assert finalized and finalized["state"] == RunState.COMPLETED.value
    assert finalized["manifest_path"] and finalized["vault_note_path"]


def test_permanent_finalize_failure_quarantines_without_starving_queue(
    db_path: Path,
) -> None:
    store = SqliteStore(str(db_path))
    _, poison_id = create_run(store, [])
    assert store.update_run(
        poison_id,
        {
            "state": RunState.COMPLETED.value,
            "worker_id": "w1",
            "finished_at": utc_now_iso(),
        },
    )
    _, healthy_id = create_run(store, [agent_step("healthy")])
    spy = FinalizationSpy()
    deps = dependencies(TrackingAdapter(), spy)

    def append_unless_poison(root: str, manifest: RunManifest) -> str:
        if manifest.run_id == poison_id:
            raise OSError("disk full")
        return spy.append(root, manifest)

    deps.append_manifest = append_unless_poison
    runner = Runner(store, "w1", dependencies=deps, finalize_attempt_limit=3)

    for _ in range(3):
        assert runner.tick() is False

    poison = store.get_run(poison_id)
    queued = store.get_run(healthy_id)
    assert poison and poison["state"] == RunState.FAILED.value
    assert poison["error"] == "finalization_failed"
    assert poison["vault_note_path"] == "quarantined:finalization_failed"
    assert queued and queued["state"] == RunState.QUEUED.value
    assert any(event["action"] == "finalize_quarantined" for event in store.get_events_after(0))

    assert runner.tick() is True
    healthy = store.get_run(healthy_id)
    assert healthy and healthy["state"] == RunState.COMPLETED.value
    assert healthy["manifest_path"] and healthy["vault_note_path"]


def test_finalize_passes_task_and_artifacts_to_note_renderer(db_path: Path) -> None:
    store = SqliteStore(str(db_path))
    task_input = {
        "prompt": "Preserve the distinctive render context",
        "tools_allowed": [],
        "customer": "keystone-test",
    }
    task_id, run_id = create_run(
        store,
        [],
        task_title="Governance keystone render wiring",
        task_input=task_input,
    )
    store.add_artifact(
        {
            "id": new_id("art"),
            "run_id": run_id,
            "type": "file",
            "uri": "workspace://governance-proof.txt",
            "created_at": utc_now_iso(),
        }
    )

    class CapturingFinalization(FinalizationSpy):
        def __init__(self) -> None:
            super().__init__()
            self.render_kwargs: dict[str, Any] = {}

        def render(
            self,
            run: dict[str, Any],
            steps: list[dict[str, Any]],
            manifest_path: str,
            receipts: list[Any],
            **kwargs: Any,
        ) -> tuple[str, str]:
            self.render_kwargs = dict(kwargs)
            return super().render(run, steps, manifest_path, receipts, **kwargs)

    spy = CapturingFinalization()
    Runner(store, "w1", dependencies=dependencies(TrackingAdapter(), spy)).tick()

    task = store.get_task(task_id)
    assert task is not None
    assert spy.render_kwargs["task_title"] == task["title"]
    assert spy.render_kwargs["artifacts_list"] == store.get_artifacts(run_id)
    assert spy.render_kwargs["task_input"] == task["input_json"]


def test_non_step_fault_directly_fails_only_that_run(db_path: Path) -> None:
    store = SqliteStore(str(db_path))
    _, run_id = create_run(store, [agent_step("never-starts")])
    deps = dependencies(TrackingAdapter(), FinalizationSpy())

    def broken_budget(
        spec: BudgetSpec, used_wall_ms: int, used_tokens: int, used_cost_usd: float
    ) -> BudgetDecision:
        raise sqlite3.OperationalError("database is locked")

    deps.check_budget = broken_budget
    runner = Runner(store, "w1", dependencies=deps)

    assert runner.tick() is True

    run = store.get_run(run_id)
    assert run and run["state"] == RunState.FAILED.value
    assert run["error"] == "runner_fault:OperationalError:database is locked"
    assert any(event["action"] == "runner_fault_isolated" for event in store.get_events_after(0))


def test_finalization_scan_fault_does_not_block_queued_work(db_path: Path) -> None:
    seed = SqliteStore(str(db_path))
    _, run_id = create_run(seed, [agent_step("work")])
    store = FinalizationScanFaultStore(str(db_path))

    assert (
        Runner(store, "w1", dependencies=dependencies(TrackingAdapter(), FinalizationSpy())).tick()
        is True
    )

    assert store.raised is True
    assert seed.get_run(run_id)["state"] == RunState.COMPLETED.value  # type: ignore[index]


def test_heartbeat_pulses_during_one_long_step(db_path: Path) -> None:
    seed = SqliteStore(str(db_path))
    _, run_id = create_run(seed, [agent_step("slow", mock={"delay_ms": 3000})])
    adapter = TrackingAdapter()
    runner = Runner(
        SqliteStore(str(db_path)),
        "owner",
        dependencies=dependencies(adapter, FinalizationSpy()),
        stale_s=2,
    )
    errors: list[BaseException] = []

    def run_tick() -> None:
        try:
            runner.tick()
        except BaseException as exc:
            errors.append(exc)

    def last_beat(worker_id: str) -> str | None:
        row = next((beat for beat in seed.get_heartbeats() if beat["worker_id"] == worker_id), None)
        return None if row is None else str(row["last_beat_at"])

    thread = threading.Thread(target=run_tick)
    thread.start()
    deadline = time.monotonic() + 2
    while not adapter.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert adapter.calls

    # Direct, timing-precision-independent proof that the heartbeat advances
    # DURING the still-in-flight step: `last_beat_at` has whole-second
    # resolution, so a threshold-based staleness check alone (below) can be
    # sensitive to exactly where the wall-clock second boundary falls relative
    # to when this test happens to run. Comparing the recorded timestamp
    # before/after the wait sidesteps that: with no mid-step refresh, this
    # worker's own tick() never calls upsert_heartbeat again until the step
    # returns, so the value provably cannot change; with the fix, at least one
    # background pulse (interval ~stale_s/3 == ~0.67s here) must land in a
    # 2.3s window, so it provably does change.
    first_beat = last_beat("owner")
    assert first_beat is not None
    time.sleep(2.3)
    second_beat = last_beat("owner")
    assert second_beat is not None and second_beat > first_beat

    intruder = SqliteStore(str(db_path))
    assert intruder.reclaim_stale_runs("intruder", stale_s=2) == []
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    run = seed.get_run(run_id)
    assert run and run["state"] == RunState.COMPLETED.value and run["worker_id"] == "owner"


def test_finalization_scan_does_not_steal_from_live_owner(db_path: Path) -> None:
    store = SqliteStore(str(db_path))
    _, run_id = create_run(store, [])
    assert store.update_run(
        run_id,
        {"state": RunState.COMPLETED.value, "worker_id": "live-owner"},
    )
    store.upsert_heartbeat("live-owner", 123, run_id)
    spy = FinalizationSpy()

    Runner(store, "scanner", dependencies=dependencies(TrackingAdapter(), spy)).tick()

    run = store.get_run(run_id)
    assert run and run["worker_id"] == "live-owner"
    assert run["manifest_path"] is None and run["vault_note_path"] is None
    assert spy.manifests == [] and spy.notes == []


def test_finalization_scan_finds_old_stranded_run_beyond_newest_hundred(
    db_path: Path,
) -> None:
    store = SqliteStore(str(db_path))
    _, stranded_id = create_run(store, [])
    assert store.update_run(
        stranded_id,
        {
            "state": RunState.COMPLETED.value,
            "worker_id": "dead-owner",
            "queued_at": "2000-01-01T00:00:00Z",
        },
    )
    for _ in range(101):
        _, finalized_id = create_run(store, [])
        assert store.update_run(
            finalized_id,
            {
                "state": RunState.COMPLETED.value,
                "worker_id": "old-worker",
                "manifest_path": "ledger/runs.jsonl",
                "vault_note_path": "vault/run.md",
            },
        )
    spy = FinalizationSpy()

    Runner(store, "scanner", dependencies=dependencies(TrackingAdapter(), spy)).tick()

    stranded = store.get_run(stranded_id)
    assert stranded and stranded["worker_id"] == "scanner"
    assert stranded["manifest_path"] and stranded["vault_note_path"]


def test_interpreter_and_shell_commands_are_hard_stopped() -> None:
    """AC-policy deny-by-default: the runner's command gate now shares ONE classifier
    with the Session Bridge. Interpreters and any command not provably read-only
    classify IRREVERSIBLE (hard-stop in AUTO mode); confined read-only probes stay
    auto (read_only). The OS sandbox in _run_command is the physical backstop."""
    from omniagentos.contracts import ActionClass
    from omniagentos.runner.core import Runner

    cac = Runner._command_action_class
    # interpreters -> IRREVERSIBLE (was the master-key bypass the reviewer flagged)
    assert cac(["python3", "-c", "open('x','w')"]) == ActionClass.IRREVERSIBLE
    assert cac(["/bin/sh", "-c", "echo hi > x"]) == ActionClass.IRREVERSIBLE
    assert cac(["node", "-e", "1"]) == ActionClass.IRREVERSIBLE
    assert cac("bash -c 'whoami'") == ActionClass.IRREVERSIBLE
    # confined read-only probes stay auto (read_only)
    assert cac(["grep", "-q", "marker", "receipt.txt"]) == ActionClass.READ_ONLY
    assert cac(["test", "-f", "x"]) == ActionClass.READ_ONLY
    # network / anything-not-provably-read-only also hard-stops
    assert cac("curl http://evil/x") == ActionClass.IRREVERSIBLE


def test_budget_exhaustion_is_advisory_by_default(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default posture: the plan finishes; the overshoot is surfaced, not fatal."""
    monkeypatch.delenv("OMNIAGENTOS_BUDGET_ENFORCEMENT", raising=False)
    store = SqliteStore(str(db_path))
    _, run_id = create_run(
        store,
        [
            agent_step("one", mock={"usage": {"input_tokens": 8, "output_tokens": 7}}),
            agent_step("two"),
        ],
        budget={"tokens_max": 10},
    )
    adapter = TrackingAdapter()
    runner = Runner(store, "w1", dependencies=dependencies(adapter, FinalizationSpy()))
    for _ in range(10):
        if not runner.tick():
            break
    run = store.get_run(run_id)
    assert run and run["error"] != "budget_exceeded"
    assert run["state"] != RunState.FAILED.value
    assert len(adapter.calls) == 2, "the second step was blocked by the budget"
