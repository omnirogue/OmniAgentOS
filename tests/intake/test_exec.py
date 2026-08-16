"""Dispatched intake tasks that do REAL work — safely, through the runner's gate.

A ``tools``-mode dispatch is tool-capable (file_read/file_write/shell + a scoped
working dir) and its single agent step is declared ``consequential``. In SUPERVISED
mode the runner's policy gate parks it in AWAITING_APPROVAL and creates an approval
row BEFORE the adapter ever runs (proven here). Under the AUTO default that same
dispatch auto-executes -- the per-action guardrail (session hooks + broker) is what
stops a delete or money-move, not a blanket park. A ``readonly`` dispatch (the
default) carries no tools and stays a plain, auto-approved generation in both modes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omniagentos.collab.store import CollabStore
from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    AgentUsage,
    BudgetDecision,
    HarnessType,
    HealthStatus,
    ResultStatus,
)
from omniagentos.intake.service import _INTAKE_TOOLS, dispatch_spec
from omniagentos.policy import PolicyMode, evaluate_action, load_policy, sandbox_for_tools
from omniagentos.runner.core import Runner, RunnerDependencies


class _RecordingAdapter:
    """A mock adapter that records whether it was ever invoked."""

    name = "mock"
    version = "1.0"

    def __init__(self) -> None:
        self.calls: list[AgentInput] = []

    def run(self, input: AgentInput) -> AgentResult:
        self.calls.append(input)
        return AgentResult(
            status=ResultStatus.OK,
            output_text="did-work",
            usage=AgentUsage(
                wall_ms=1,
                turns=1,
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.0,
                estimated=True,
                source="estimator",
            ),
        )

    def cancel(self, session_ref: str) -> bool:  # pragma: no cover - unused
        return True

    def health(self) -> HealthStatus:  # pragma: no cover - unused
        return HealthStatus(healthy=True)


def _real_policy_deps(adapter: _RecordingAdapter, cfg: Any) -> RunnerDependencies:
    """Runner deps with the REAL policy evaluation (so consequential parks), but a
    mock adapter and stub ledger/vault seams."""
    return RunnerDependencies(
        evaluate_policy=lambda action: evaluate_action(action, cfg),
        sandbox_for_tools=lambda harness, tools: sandbox_for_tools(harness, tools, cfg),
        check_budget=lambda spec, w, t, c: BudgetDecision(allowed=True),
        resolve_adapter=lambda harness: adapter,
        append_manifest=lambda root, manifest: str(Path(root) / "runs.jsonl"),
        render_run_note=lambda run, steps, manifest_path, receipts, **kw: (
            f"runs/{run['id']}.md",
            "note",
        ),
        write_note=lambda root, relpath, content: str(Path(root) / relpath),
    )


def _make_runner(store: Any, adapter: _RecordingAdapter, cfg: Any, tmp_path: Path) -> Runner:
    return Runner(
        store,
        "w-intake",
        dependencies=_real_policy_deps(adapter, cfg),
        ledger_dir=str(tmp_path / "ledger"),
        vault_dir=str(tmp_path / "vault"),
        workspace_base=str(tmp_path / "ws"),
    )


def _spec() -> Any:
    from omniagentos.intake.contracts import RefinedSpec

    return RefinedSpec(
        title="Write a hello file",
        description="Create hello.txt with the word hi.",
        acceptance_criteria=["hello.txt exists"],
    )


def test_tools_dispatch_parks_for_approval(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    db = str(tmp_path / "exec.db")
    collab = CollabStore(db)
    store = collab._store
    # SUPERVISED mode: the consequential dispatch must park (regression coverage
    # for the legacy gate that AUTO mode deliberately relaxes).
    cfg = load_policy().model_copy(update={"mode": PolicyMode.SUPERVISED})

    result = dispatch_spec(
        store, collab, cfg, _spec(), harness=HarnessType.MOCK.value, execute="tools"
    )
    run_id = str(result["run_id"])
    task_id = str(result["task_id"])

    # The dispatch is tools-capable and scoped to a per-task scratch dir.
    assert result["execute"] == "tools"
    working_dir = result["working_dir"]
    assert working_dir is not None
    expected = tmp_path / "var" / "intake-workspace" / task_id
    assert Path(working_dir) == expected
    assert expected.is_dir()  # created up front, never a home/unscoped dir

    # The task carries the real-work tool grant.
    task = store.get_task(task_id)
    assert task is not None
    import json

    assert json.loads(task["input_json"])["tools_allowed"] == _INTAKE_TOOLS

    adapter = _RecordingAdapter()
    runner = _make_runner(store, adapter, cfg, tmp_path)
    for _ in range(20):
        if not runner.tick():
            break

    # The consequential action PARKED for approval: no auto-execution.
    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == "awaiting_approval"
    assert adapter.calls == []  # the adapter (real work) never ran

    # An approval row exists in the queue the dashboard surfaces.
    pending = store.list_approvals(state="pending")
    rows = [a for a in pending if a["run_id"] == run_id]
    assert len(rows) == 1
    assert rows[0]["action_class"] == "consequential"


def test_readonly_dispatch_stays_readonly(tmp_path: Path) -> None:
    db = str(tmp_path / "ro.db")
    collab = CollabStore(db)
    store = collab._store
    cfg = load_policy()

    result = dispatch_spec(store, collab, cfg, _spec(), harness=HarnessType.MOCK.value)
    run_id = str(result["run_id"])
    task_id = str(result["task_id"])

    # Default posture: read-only, no tool grant, no scoped working dir.
    assert result["execute"] == "readonly"
    assert result["working_dir"] is None
    task = store.get_task(task_id)
    assert task is not None
    import json

    assert "tools_allowed" not in json.loads(task["input_json"])

    adapter = _RecordingAdapter()
    runner = _make_runner(store, adapter, cfg, tmp_path)
    for _ in range(20):
        if not runner.tick():
            break

    # A read-only run auto-approves and completes; the adapter ran with NO tools
    # and a read_only sandbox — it never parked for approval.
    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == "completed"
    assert len(adapter.calls) == 1
    assert adapter.calls[0].tools_allowed == []
    assert adapter.calls[0].metadata["sandbox"]["level"] == "read_only"
    assert store.list_approvals(state="pending") == []


def test_tools_dispatch_uses_project_dir(tmp_path: Path) -> None:
    from omniagentos.projects import ProjectStore

    db = str(tmp_path / "proj.db")
    collab = CollabStore(db)
    store = collab._store
    cfg = load_policy()

    project_root = tmp_path / "repo"
    project_root.mkdir()
    project = ProjectStore(store).create_project({"name": "demo", "root_dirs": [str(project_root)]})

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        harness=HarnessType.MOCK.value,
        project_id=str(project["id"]),
        execute="tools",
    )

    # A project-scoped tools dispatch works in the project's own root dir.
    assert result["working_dir"] == str(project_root)
