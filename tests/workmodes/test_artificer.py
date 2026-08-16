"""TN.2 — the artificer path: a non-code deliverable never touches the repo.

``test_repo_write_is_named_as_such`` is the load-bearing one. The failure this
package exists to stop is a copywriting task handed a repo-scoped worker, and
the guard has to say *that*, not a generic "outside scope" — the two have
different fixes and only one of them is an incident.

``test_symlink_out_of_the_artifact_root_is_caught`` is the one that proves the
guard did not grow its own path algebra: containment goes through
``scope.paths.resolve_into_realm``, which resolves symlinks, so the classic
"write through a symlink" escape lands OUT of scope rather than passing a
lexical prefix check.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omniagentos.contracts import SandboxLevel, TaskMode
from omniagentos.workmodes.artificer import (
    ArtifactScopeViolation,
    artificer_prompt,
    enforce_writes,
    guard_writes,
    plan_artificer,
    worker_shape,
)
from omniagentos.workmodes.modes import provision_roots


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    return root


@pytest.fixture
def var(tmp_path: Path) -> Path:
    base = tmp_path / "repo" / "var"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _plan(mode: TaskMode, repo: Path, var: Path, **kwargs: object):
    plan = plan_artificer(mode, "tsk_1", base=str(var), repo_root=str(repo), **kwargs)  # type: ignore[arg-type]
    provision_roots(plan.roots)
    return plan


def test_content_working_dir_is_the_artifact_root_not_the_repo(repo: Path, var: Path) -> None:
    plan = _plan(TaskMode.CONTENT, repo, var)
    assert plan.working_dir == str(var / "artifacts" / "tsk_1")
    assert plan.repo_write_allowed is False
    assert plan.is_artificer is True
    assert str(repo) not in plan.write_roots


def test_intake_working_dir_is_the_mutable_workspace(repo: Path, var: Path) -> None:
    plan = _plan(TaskMode.INTAKE_PROCESSING, repo, var)
    assert plan.working_dir == str(var / "workspace" / "tsk_1")
    assert plan.read_only_roots == (str(var / "inputs" / "tsk_1"),)


def test_code_keeps_the_repo_and_the_guard_abstains(repo: Path, var: Path) -> None:
    plan = _plan(TaskMode.CODE, repo, var)
    assert plan.working_dir == str(repo)
    assert plan.repo_write_allowed is True
    verdict = guard_writes([str(repo / "src" / "app.py")], plan)
    assert verdict.applicable is False
    assert verdict.ok is True
    assert verdict.findings == ()


def test_write_inside_the_artifact_root_is_allowed(repo: Path, var: Path) -> None:
    plan = _plan(TaskMode.REPORT, repo, var)
    target = os.path.join(plan.working_dir, "q1", "summary.md")
    verdict = guard_writes([target], plan)
    assert verdict.ok is True
    assert verdict.findings[0].kind == "ok"
    assert verdict.findings[0].root == plan.working_dir


def test_repo_write_is_named_as_such(repo: Path, var: Path) -> None:
    """The exact failure TN.2 exists to stop, with its own violation kind."""
    plan = _plan(TaskMode.CONTENT, repo, var)
    verdict = guard_writes([str(repo / "src" / "app.py")], plan)
    assert verdict.applicable is True
    assert verdict.ok is False
    assert [f.kind for f in verdict.findings] == ["repo_write"]
    assert verdict.repo_writes[0].path.endswith("src/app.py")


def test_write_outside_every_root_is_a_violation(repo: Path, var: Path, tmp_path: Path) -> None:
    plan = _plan(TaskMode.IMAGE, repo, var)
    verdict = guard_writes([str(tmp_path / "elsewhere" / "hero.png")], plan)
    assert [f.kind for f in verdict.findings] == ["outside_roots"]
    assert verdict.ok is False


def test_read_only_root_write_is_a_violation(repo: Path, var: Path) -> None:
    """intake_processing may READ its uploads; writing them destroys the original."""
    plan = _plan(TaskMode.INTAKE_PROCESSING, repo, var)
    target = os.path.join(plan.roots.inputs or "", "invoice.pdf")
    verdict = guard_writes([target], plan)
    assert [f.kind for f in verdict.findings] == ["read_only_root"]
    assert verdict.ok is False
    # ...while the workspace and artifacts roots are writable.
    ok = guard_writes([os.path.join(plan.roots.workspace or "", "scratch.txt")], plan)
    assert ok.ok is True


def test_symlink_out_of_the_artifact_root_is_caught(repo: Path, var: Path, tmp_path: Path) -> None:
    plan = _plan(TaskMode.REPORT, repo, var)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = Path(plan.working_dir) / "escape"
    link.symlink_to(outside, target_is_directory=True)
    verdict = guard_writes([str(link / "leak.md")], plan)
    assert [f.kind for f in verdict.findings] == ["outside_roots"]


def test_granted_root_inside_the_repo_is_dropped(repo: Path, var: Path) -> None:
    """A grant is not a loophole: a non-code mode may not be granted repo paths."""
    plan = plan_artificer(
        TaskMode.CONTENT,
        "tsk_1",
        granted_roots=[str(repo / "src")],
        base=str(var),
        repo_root=str(repo),
    )
    assert str(repo / "src") not in plan.write_roots
    assert any("inside the repo" in reason for _path, reason in plan.dropped_roots)


def test_secret_adjacent_granted_root_is_dropped(
    repo: Path, var: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reuses policy.dir_grants.references_secret_dir; a grant is not a loophole."""
    grant = tmp_path / "creds"
    grant.mkdir()
    monkeypatch.setattr(
        "omniagentos.policy.dir_grants.references_secret_dir",
        lambda path: str(grant) in path,
    )
    plan = plan_artificer(
        TaskMode.CONTENT,
        "tsk_1",
        granted_roots=[str(grant)],
        base=str(var),
        repo_root=str(repo),
    )
    assert os.path.realpath(str(grant)) not in plan.write_roots
    assert any("secret" in reason for _path, reason in plan.dropped_roots)


def test_an_unusable_secret_registry_fails_closed(
    repo: Path, var: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Could not verify" and "not allowed" must have the same consequence."""
    grant = tmp_path / "client"
    grant.mkdir()

    def boom(_path: str) -> bool:
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr("omniagentos.policy.dir_grants.references_secret_dir", boom)
    plan = plan_artificer(
        TaskMode.CONTENT,
        "tsk_1",
        granted_roots=[str(grant)],
        base=str(var),
        repo_root=str(repo),
    )
    assert os.path.realpath(str(grant)) not in plan.write_roots
    assert any("unavailable" in reason for _path, reason in plan.dropped_roots)


def test_granted_root_outside_the_repo_is_kept(repo: Path, var: Path, tmp_path: Path) -> None:
    grant = tmp_path / "client-deliverables"
    grant.mkdir()
    plan = plan_artificer(
        TaskMode.CONTENT,
        "tsk_1",
        granted_roots=[str(grant)],
        base=str(var),
        repo_root=str(repo),
    )
    assert os.path.realpath(str(grant)) in plan.write_roots
    verdict = guard_writes([str(grant / "ads.md")], plan)
    assert verdict.ok is True


def test_artifact_roots_under_var_survive_the_repo_filter(repo: Path, var: Path) -> None:
    """var/ lives INSIDE the repo; the mode roots must not be dropped as repo writes."""
    plan = _plan(TaskMode.REPORT, repo, var)
    assert plan.working_dir.startswith(str(repo))
    verdict = guard_writes([os.path.join(plan.working_dir, "r.md")], plan)
    assert verdict.ok is True


def test_target_location_is_parsed_from_the_goal(
    repo: Path, var: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reuses intake.fastlane.parse_target_location rather than re-parsing goals."""
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setattr(
        "omniagentos.intake.fastlane.parse_target_location", lambda _goal: str(desktop)
    )
    plan = plan_artificer(
        TaskMode.REPORT,
        "tsk_1",
        goal="put the report on my desktop",
        base=str(var),
        repo_root=str(repo),
    )
    assert os.path.realpath(str(desktop)) in plan.write_roots


def test_enforce_raises_only_when_the_gate_says_enforce(repo: Path, var: Path) -> None:
    plan = _plan(TaskMode.CONTENT, repo, var)
    offending = [str(repo / "src" / "app.py")]

    # Default (off) and observe: verdict returned, nothing refused.
    assert enforce_writes(offending, plan, gate="off").ok is False
    assert enforce_writes(offending, plan, gate="observe").ok is False

    with pytest.raises(ArtifactScopeViolation) as excinfo:
        enforce_writes(offending, plan, gate="enforce")
    assert excinfo.value.verdict.repo_writes


def test_enforce_never_raises_for_code(repo: Path, var: Path) -> None:
    plan = _plan(TaskMode.CODE, repo, var)
    assert enforce_writes([str(repo / "anything.py")], plan, gate="enforce").ok is True


def test_worker_shape_points_away_from_the_repo(repo: Path, var: Path) -> None:
    plan = _plan(TaskMode.IMAGE, repo, var)
    shape = worker_shape(plan)
    assert shape.working_dir == plan.working_dir
    assert shape.repo_scoped is False
    assert shape.sandbox_level is SandboxLevel.WORKSPACE_WRITE
    assert str(repo) not in shape.extra_dirs


def test_prompt_names_the_root_and_the_prohibition(repo: Path, var: Path) -> None:
    plan = _plan(TaskMode.CONTENT, repo, var)
    text = artificer_prompt("write three ad variants", plan)
    assert plan.working_dir in text
    assert str(repo) in text
    assert "content task" in text


def test_prompt_for_code_is_unchanged(repo: Path, var: Path) -> None:
    """SHIP DARK: a code task's prompt is the goal, byte for byte."""
    plan = _plan(TaskMode.CODE, repo, var)
    assert artificer_prompt("fix the bug", plan) == "fix the bug"


def test_unresolvable_path_fails_closed(repo: Path, var: Path) -> None:
    plan = _plan(TaskMode.REPORT, repo, var)
    verdict = guard_writes(["", "   "], plan)
    assert [f.kind for f in verdict.findings] == ["unresolvable", "unresolvable"]
    assert verdict.ok is False
