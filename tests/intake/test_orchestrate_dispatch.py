"""``execute="orchestrate"`` — the thin intake path into the Orchestrator library.

Default OFF: the existing readonly/tools/session modes are untouched. When selected,
``dispatch_spec`` hands the composed spec to the Orchestrator (injected here as a stub
so no real planning/spawn happens), creates a visible board card, and returns the
orchestration summary with the priority/pins threaded through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.intake.contracts import RefinedSpec
from omniagentos.intake.service import dispatch_spec
from omniagentos.policy import load_policy
from omniagentos.projects import ProjectStore


@dataclass
class _FakeResult:
    run_id: str = "orch_test0001"
    status: str = "done"
    tasks: list[Any] = field(default_factory=lambda: [object(), object()])
    spec_note_path: str = "/vault/orchestration/x.md"
    escalations: list[Any] = field(default_factory=list)


@dataclass
class _StubOrchestrateRunner:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        goal: str,
        *,
        priority: str = "balanced",
        pins: dict[str, Any] | None = None,
        working_dir: str | None = None,
        project_id: str | None = None,
        run_id: str | None = None,
        granted_roots: list[str] | None = None,
    ) -> Any:
        self.calls.append(
            {
                "goal": goal,
                "priority": priority,
                "pins": pins,
                "working_dir": working_dir,
                "project_id": project_id,
                "run_id": run_id,
                "granted_roots": granted_roots,
            }
        )
        return _FakeResult(run_id=run_id or "orch_test0001")


def _stores(tmp_path: Path) -> tuple[Any, CollabStore, Any]:
    collab = CollabStore(str(tmp_path / "orch.db"))
    return collab._store, collab, load_policy()


def _spec(**overrides: Any) -> RefinedSpec:
    base: dict[str, Any] = {
        "title": "Ship the greeter",
        "description": "Refactor and test the greeter module.",
        "acceptance_criteria": ["greeter refactored", "tests pass"],
    }
    base.update(overrides)
    return RefinedSpec(**base)


def test_orchestrate_dispatch_delegates_to_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    store, collab, cfg = _stores(tmp_path)
    runner = _StubOrchestrateRunner()

    result = dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        execute="orchestrate",
        priority="quality",
        pins={"planner_model": "opus"},
        orchestrate_runner=runner,
    )

    # Delegated exactly once, with the composed spec as the goal + knobs threaded.
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert "Ship the greeter" in call["goal"]
    assert "greeter refactored" in call["goal"]
    assert call["priority"] == "quality"
    assert call["pins"] == {"planner_model": "opus"}

    # No queued run / session is created on this path; the library owns execution.
    assert result["execute"] == "orchestrate"
    assert result["run_id"].startswith("orch_")
    assert result["session_id"] is None
    assert result["orchestration"]["status"] == "done"
    assert result["orchestration"]["task_count"] == 2
    assert result["orchestration"]["priority"] == "quality"

    project = ProjectStore(store).get_project(str(result["project_id"]))
    assert project is not None
    assert project["root_dirs"] == [
        str(tmp_path / "var" / "projects" / str(result["project_id"]) / "workspace")
    ]
    assert runner.calls[0]["working_dir"] == project["root_dirs"][0]

    # A board card is created (dashboard visibility) and linked to the run.
    card = collab.get_board_task(result["board_task"]["id"])
    assert card is not None
    assert card["result_ref"] == result["run_id"]


def test_orchestrate_dispatch_passes_project_working_dir(tmp_path: Path) -> None:
    store, collab, cfg = _stores(tmp_path)
    runner = _StubOrchestrateRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectStore(store).create_project({"name": "greeter", "root_dirs": [str(repo)]})

    dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        project_id=str(project["id"]),
        execute="orchestrate",
        orchestrate_runner=runner,
    )

    assert runner.calls[0]["working_dir"] == str(repo)
    assert runner.calls[0]["project_id"] == str(project["id"])
    # Default knob when unspecified.
    assert runner.calls[0]["priority"] == "balanced"
    # Single-root project: no *project* scope beyond working_dir. AUTO-APPROVE
    # Phase 1 may still merge standing roots (Desktop, var/, …).
    import os

    granted = runner.calls[0]["granted_roots"] or []
    real = [os.path.realpath(g) for g in granted]
    assert os.path.realpath(str(repo)) not in real  # working_dir excluded
    assert os.path.realpath(os.path.expanduser("~/.ssh")) not in real
    assert not any(r.startswith(os.path.realpath(os.path.expanduser("~/.ssh"))) for r in real)


def test_orchestrate_dispatch_threads_full_granted_scope(tmp_path: Path) -> None:
    """FIX 6: orchestrate resolves the project's FULL granted scope (root_dirs +
    allowed_dirs beyond working_dir) SERVER-SIDE and threads it into run_orchestration,
    so every executor session it spawns is frozen with the same scope an intake session
    gets (not confined to working_dir)."""
    import os

    store, collab, cfg = _stores(tmp_path)
    runner = _StubOrchestrateRunner()
    primary = tmp_path / "repo"
    second = tmp_path / "repo2"
    allowed = tmp_path / "shared"
    for directory in (primary, second, allowed):
        directory.mkdir()
    project = ProjectStore(store).create_project(
        {
            "name": "multiroot",
            "root_dirs": [str(primary), str(second)],
            "allowed_dirs": [str(allowed)],
        }
    )

    dispatch_spec(
        store,
        collab,
        cfg,
        _spec(),
        project_id=str(project["id"]),
        execute="orchestrate",
        orchestrate_runner=runner,
    )

    # working_dir is root_dirs[0]; granted_roots is the REST of the scope, realpathed
    # and excluding working_dir -- plus standing roots (AUTO-APPROVE Phase 1).
    assert runner.calls[0]["working_dir"] == str(primary)
    granted = runner.calls[0]["granted_roots"] or []
    real = [os.path.realpath(g) for g in granted]
    assert os.path.realpath(str(second)) in real
    assert os.path.realpath(str(allowed)) in real
    assert os.path.realpath(os.path.expanduser("~/.ssh")) not in real


def test_orchestrate_mode_does_not_disturb_readonly(tmp_path: Path) -> None:
    # The default mode still creates a queued run (existing behaviour untouched).
    store, collab, cfg = _stores(tmp_path)
    result = dispatch_spec(store, collab, cfg, _spec(), execute="readonly")
    assert result["execute"] == "readonly"
    assert result["run_id"] is not None
    assert "orchestration" not in result
