"""Drive Access for projects (W4): the runner threads a run's project
root_dirs + allowed_dirs onto AgentInput.metadata["extra_dirs"], deduped and
excluding working_dir, so an adapter can --add-dir every granted directory
(including a granted Drive subfolder -- see tests/provision/test_drive.py and
tests/adapters/test_claude.py for the two neighboring halves of this seam).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import (
    ActionClass,
    AgentInput,
    BudgetDecision,
    HarnessType,
    PolicyDecision,
    RunState,
    SandboxSpec,
    TaskState,
    new_id,
    utc_now_iso,
)
from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.mock_adapter import MockAdapter
from omniagentos.projects import ProjectStore
from omniagentos.runner.core import Runner, RunnerDependencies


class TrackingAdapter(MockAdapter):
    def __init__(self) -> None:
        self.calls: list[AgentInput] = []

    def run(self, input: AgentInput) -> Any:
        self.calls.append(input)
        return super().run(input)


def _dependencies(adapter: TrackingAdapter) -> RunnerDependencies:
    return RunnerDependencies(
        evaluate_policy=lambda _action: PolicyDecision(requires_approval=False),
        sandbox_for_tools=lambda _harness, tools: SandboxSpec(
            level="workspace_write" if "file_write" in tools else "read_only"
        ),
        check_budget=lambda *_args: BudgetDecision(allowed=True),
        resolve_adapter=lambda _harness: adapter,
        append_manifest=lambda root, _manifest: str(Path(root) / "runs.jsonl"),
        render_run_note=lambda run, _steps, _manifest, _receipts, **_kwargs: (
            f"runs/{run['id']}.md",
            "done",
        ),
        write_note=lambda root, relpath, _content: str(Path(root) / relpath),
    )


def _create_run(
    store: SqliteStore,
    *,
    project_id: str | None,
    working_dir: str | None,
) -> str:
    now = utc_now_iso()
    task_id = new_id("tsk")
    run_id = new_id("run")
    store.create_task(
        {
            "id": task_id,
            "discipline_id": "code-changes",
            "title": "drive access dir-threading test",
            "project_id": project_id,
            "input_json": json.dumps(
                {"prompt": "do the thing", "tools_allowed": [], "working_dir": working_dir}
            ),
            "acceptance_json": "{}",
            "state": TaskState.QUEUED.value,
            "risk": "low",
            "created_at": now,
            "updated_at": now,
        }
    )
    plan = [
        {
            "name": "step",
            "kind": "agent",
            "action_class": ActionClass.SANDBOXED_CREATION.value,
            "params": {"adapter": "mock"},
        }
    ]
    store.enqueue_run(
        {
            "id": run_id,
            "task_id": task_id,
            "discipline_id": "code-changes",
            "harness": HarnessType.MOCK.value,
            "state": RunState.QUEUED.value,
            "plan_json": json.dumps(plan),
            "budget_json": "{}",
            "trace_id": f"trace-{run_id}",
            "queued_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    return run_id


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    db_path = tmp_path / "runner.db"
    migrate(str(db_path))
    return SqliteStore(str(db_path))


def test_extra_dirs_empty_for_unscoped_run(store: SqliteStore) -> None:
    """Pre-W4 behavior: no project attached -> metadata["extra_dirs"] == [].

    working_dir is None here: an unscoped run is confined to its per-run
    workspace, so the fix6 scope check would (correctly) reject any out-of-scope
    working_dir. The extra_dirs==[] invariant is what this test pins.
    """
    _create_run(store, project_id=None, working_dir=None)
    adapter = TrackingAdapter()

    assert Runner(store, "w1", dependencies=_dependencies(adapter)).tick()

    assert adapter.calls[0].metadata["extra_dirs"] == []


def test_agent_working_dir_out_of_scope_is_rejected(store: SqliteStore, tmp_path: Path) -> None:
    """fix6 (control-plane create_run escape): a project-scoped agent step whose
    working_dir escapes the granted scope (workspace ∪ project root_dirs/
    allowed_dirs) is REJECTED before working_dir can become a sandbox write-root.
    """
    project_root = tmp_path / "repo"
    project_root.mkdir()
    project = ProjectStore(store).create_project(
        {"name": "p", "root_dirs": [str(project_root)], "allowed_dirs": [str(project_root)]}
    )
    # Stands in for ~/.ssh: outside the workspace AND every project grant.
    outside = tmp_path / "dot-ssh"
    run_id = _create_run(store, project_id=str(project["id"]), working_dir=str(outside))
    adapter = TrackingAdapter()

    Runner(store, "w1", dependencies=_dependencies(adapter)).tick()

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == RunState.FAILED.value
    assert "working_dir_out_of_scope" in str(run["error"])
    assert adapter.calls == []  # the adapter (a sandbox write-root) never ran


def test_agent_working_dir_rejected_for_unscoped_run(store: SqliteStore, tmp_path: Path) -> None:
    """An unscoped run may only write its per-run workspace; the create_run exploit
    shape (unscoped + arbitrary working_dir) is rejected."""
    run_id = _create_run(store, project_id=None, working_dir=str(tmp_path / "elsewhere"))
    adapter = TrackingAdapter()

    Runner(store, "w1", dependencies=_dependencies(adapter)).tick()

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == RunState.FAILED.value
    assert "working_dir_out_of_scope" in str(run["error"])
    assert adapter.calls == []


def test_agent_in_scope_working_dir_still_runs(store: SqliteStore, tmp_path: Path) -> None:
    """The fix must not break legitimate project-scoped writes: a working_dir
    inside (a subdir of) a granted project dir runs normally."""
    project_root = tmp_path / "repo"
    project_root.mkdir()
    subdir = project_root / "sub"
    subdir.mkdir()
    project = ProjectStore(store).create_project(
        {"name": "p", "root_dirs": [str(project_root)], "allowed_dirs": [str(project_root)]}
    )
    run_id = _create_run(store, project_id=str(project["id"]), working_dir=str(subdir))
    adapter = TrackingAdapter()

    Runner(store, "w1", dependencies=_dependencies(adapter)).tick()

    run = store.get_run(run_id)
    assert run is not None
    assert run["state"] == RunState.COMPLETED.value
    assert adapter.calls and adapter.calls[0].working_dir == str(subdir)


def test_extra_dirs_collects_project_dirs_excluding_working_dir(
    store: SqliteStore, tmp_path: Path
) -> None:
    working_dir = str(tmp_path / "work")
    granted = str(tmp_path / "drive" / "granted-subfolder")
    Path(granted).mkdir(parents=True)
    project = ProjectStore(store).create_project(
        {
            "name": "p",
            "root_dirs": [working_dir, granted],
            "allowed_dirs": [working_dir, granted],
        }
    )
    _create_run(store, project_id=str(project["id"]), working_dir=working_dir)
    adapter = TrackingAdapter()

    assert Runner(store, "w1", dependencies=_dependencies(adapter)).tick()

    extra = adapter.calls[0].metadata["extra_dirs"]
    assert extra == [granted]
    assert working_dir not in extra


def test_extra_dirs_dedupes_root_and_allowed_overlap(store: SqliteStore, tmp_path: Path) -> None:
    shared = str(tmp_path / "shared")
    Path(shared).mkdir()
    project = ProjectStore(store).create_project(
        {"name": "p", "root_dirs": [shared], "allowed_dirs": [shared]}
    )
    _create_run(store, project_id=str(project["id"]), working_dir=None)
    adapter = TrackingAdapter()

    assert Runner(store, "w1", dependencies=_dependencies(adapter)).tick()

    assert adapter.calls[0].metadata["extra_dirs"] == [shared]


def test_extra_dirs_empty_for_project_with_no_grants(store: SqliteStore) -> None:
    project = ProjectStore(store).create_project({"name": "bare"})
    _create_run(store, project_id=str(project["id"]), working_dir=None)
    adapter = TrackingAdapter()

    assert Runner(store, "w1", dependencies=_dependencies(adapter)).tick()

    assert adapter.calls[0].metadata["extra_dirs"] == []


def test_extra_dirs_empty_for_unresolvable_project_id_degrades_safely(store: SqliteStore) -> None:
    """An unresolvable project_id must degrade to [] rather than raise.

    `tasks.project_id` has a real FK to `projects`, so this can't be reached
    by creating a task against a made-up id (the DB itself rejects that
    insert) -- the realistic case this guards is a project row disappearing
    between task creation and run execution. Exercised directly against the
    method rather than a full run, which is the precise unit for that guard.
    """
    runner = Runner(store, "w1", dependencies=_dependencies(TrackingAdapter()))

    assert runner._project_extra_dirs("proj_does_not_exist", None) == []
