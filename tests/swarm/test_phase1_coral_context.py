"""B2: bounded, root-confined CORAL context in swarm worktrees."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from omniagentos.swarm.worktrees import (
    CORAL_CONTEXT_ENV,
    CORAL_HUB_DIR,
    CORAL_KIND_LIMITS,
    CORAL_MAX_FILE_BYTES,
    SubprocessSwarmWorktrees,
    coral_context_mode,
    coral_hub_references,
    discover_coral_sources,
    provision_coral_hub,
)
from tests.swarm.scheduler_fakes import init_git_repo

RUN_ID = "swr_coral"


def _git(cwd: Path | str, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _shared_root(tmp_path: Path) -> Path:
    root = tmp_path / "shared-coral"
    for kind in CORAL_KIND_LIMITS:
        (root / kind).mkdir(parents=True)
    (root / "skills" / "python.md").write_text("Use pathlib.", encoding="utf-8")
    (root / "playbooks" / "review.md").write_text("Run targeted tests.", encoding="utf-8")
    (root / "runs" / "recent.md").write_text("The last run passed.", encoding="utf-8")
    return root


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    init_git_repo(repo)
    return repo


def test_coral_context_mode_defaults_off_and_accepts_tri_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CORAL_CONTEXT_ENV, raising=False)
    assert coral_context_mode() == "off"
    for mode in ("off", "shadow", "enforce"):
        monkeypatch.setenv(CORAL_CONTEXT_ENV, mode.upper())
        assert coral_context_mode() == mode
    monkeypatch.setenv(CORAL_CONTEXT_ENV, "enfroce")
    assert coral_context_mode() == "off"


def test_off_is_strict_noop_even_for_missing_paths(tmp_path: Path) -> None:
    assert (
        provision_coral_hub(
            tmp_path / "missing-worker",
            tmp_path / "missing-root",
            skill_bodies=("skills/missing.md",),
            mode="off",
        )
        == ()
    )
    assert not (tmp_path / "missing-worker").exists()


def test_enforce_provisions_prompt_ready_bounded_links_inside_approved_root(
    tmp_path: Path,
) -> None:
    worker = tmp_path / "worker"
    worker.mkdir()
    root = _shared_root(tmp_path)

    references = provision_coral_hub(
        worker,
        root,
        skill_bodies=("skills/python.md",),
        playbook_excerpts=("playbooks/review.md",),
        recent_run_notes=("runs/recent.md",),
        mode="enforce",
    )

    assert [reference.worker_path for reference in references] == [
        "var/coral/skills/python.md",
        "var/coral/playbooks/review.md",
        "var/coral/runs/recent.md",
    ]
    approved = root.resolve()
    for reference in references:
        link = worker / reference.worker_path
        assert link.is_symlink()
        assert link.resolve().is_relative_to(approved)
        assert reference.source == link.resolve()
        assert reference.size_bytes == link.stat().st_size


def test_shadow_returns_references_without_mutating_worker_tree(tmp_path: Path) -> None:
    worker = tmp_path / "worker"
    worker.mkdir()
    untouched = worker / "existing.txt"
    untouched.write_text("keep", encoding="utf-8")
    root = _shared_root(tmp_path)
    before = sorted(path.relative_to(worker).as_posix() for path in worker.rglob("*"))
    symlinks_before = sorted(
        path.relative_to(worker).as_posix() for path in worker.rglob("*") if path.is_symlink()
    )

    references = provision_coral_hub(
        worker,
        root,
        skill_bodies=("skills/python.md",),
        playbook_excerpts=("playbooks/review.md",),
        recent_run_notes=("runs/recent.md",),
        mode="shadow",
    )

    after = sorted(path.relative_to(worker).as_posix() for path in worker.rglob("*"))
    symlinks_after = sorted(
        path.relative_to(worker).as_posix() for path in worker.rglob("*") if path.is_symlink()
    )
    assert len(references) == 3
    assert before == after
    assert symlinks_before == symlinks_after
    assert not (worker / CORAL_HUB_DIR).exists()


def test_validates_complete_set_before_creating_hub(tmp_path: Path) -> None:
    worker = tmp_path / "worker"
    worker.mkdir()
    root = _shared_root(tmp_path)

    with pytest.raises(ValueError, match="traversal-free"):
        provision_coral_hub(
            worker,
            root,
            skill_bodies=("skills/python.md", "skills/../playbooks/review.md"),
            mode="shadow",
        )

    assert not (worker / CORAL_HUB_DIR).exists()


def test_rejects_symlink_escape_from_approved_shared_root(tmp_path: Path) -> None:
    worker = tmp_path / "worker"
    worker.mkdir()
    root = _shared_root(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    os.symlink(outside, root / "skills" / "escape.md")

    with pytest.raises(ValueError, match="escapes approved shared root"):
        provision_coral_hub(
            worker,
            root,
            skill_bodies=("skills/escape.md",),
            mode="enforce",
        )

    assert not (worker / CORAL_HUB_DIR).exists()


def test_rejects_worker_hub_parent_symlink_escape(tmp_path: Path) -> None:
    worker = tmp_path / "worker"
    worker.mkdir()
    root = _shared_root(tmp_path)
    outside = tmp_path / "outside-worker"
    outside.mkdir()
    os.symlink(outside, worker / "var")

    with pytest.raises(ValueError, match="hub parent escapes worker directory"):
        provision_coral_hub(
            worker,
            root,
            skill_bodies=("skills/python.md",),
            mode="enforce",
        )

    assert not (outside / "coral").exists()


def test_allows_symlink_whose_resolved_file_remains_inside_root(tmp_path: Path) -> None:
    worker = tmp_path / "worker"
    worker.mkdir()
    root = _shared_root(tmp_path)
    os.symlink(root / "skills" / "python.md", root / "skills" / "alias.md")

    (reference,) = provision_coral_hub(
        worker,
        root,
        skill_bodies=("skills/alias.md",),
        mode="shadow",
    )

    assert reference.source == (root / "skills" / "python.md").resolve()
    assert not (worker / reference.worker_path).exists()


def test_rejects_more_than_the_per_kind_bound(tmp_path: Path) -> None:
    worker = tmp_path / "worker"
    worker.mkdir()
    root = _shared_root(tmp_path)
    requested: list[str] = []
    for index in range(CORAL_KIND_LIMITS["skills"] + 1):
        name = f"skill-{index:02}.md"
        (root / "skills" / name).write_text(str(index), encoding="utf-8")
        requested.append(f"skills/{name}")

    with pytest.raises(ValueError, match="item limit"):
        provision_coral_hub(worker, root, skill_bodies=requested, mode="enforce")

    assert not (worker / CORAL_HUB_DIR).exists()


def test_rejects_oversized_context_file(tmp_path: Path) -> None:
    worker = tmp_path / "worker"
    worker.mkdir()
    root = _shared_root(tmp_path)
    (root / "skills" / "oversized.md").write_bytes(b"x" * (CORAL_MAX_FILE_BYTES + 1))

    with pytest.raises(ValueError, match="byte limit"):
        provision_coral_hub(
            worker,
            root,
            skill_bodies=("skills/oversized.md",),
            mode="shadow",
        )

    assert not (worker / CORAL_HUB_DIR).exists()


def test_discovery_is_deterministic_and_bounded(tmp_path: Path) -> None:
    root = _shared_root(tmp_path)
    for index in range(CORAL_KIND_LIMITS["runs"] + 3):
        (root / "runs" / f"note-{index:02}.md").write_text(str(index), encoding="utf-8")
    (root / "runs" / "nested").mkdir()
    (root / "runs" / "nested" / "hidden.md").write_text("hidden", encoding="utf-8")

    sources = discover_coral_sources(root)

    assert len(sources["runs"]) == CORAL_KIND_LIMITS["runs"]
    assert sources["runs"] == tuple(sorted(sources["runs"], key=str.casefold))
    assert all("nested" not in source for source in sources["runs"])


def test_worktree_binding_shadow_observes_without_provisioning_worker_hub(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    root = _shared_root(tmp_path)
    worktrees = SubprocessSwarmWorktrees(
        var_root=tmp_path / "var" / "swarm",
        dep_link_dirs=(),
        coral_shared_root=root,
        coral_mode="shadow",
        lock_retry_sleep=0,
    )

    info = worktrees.create(str(repo), RUN_ID, "task-a", _git(repo, "rev-parse", "HEAD"))

    references = coral_hub_references(info.path, root, mode="shadow")
    assert references
    assert all(not (Path(info.path) / ref.worker_path).exists() for ref in references)
    assert not (Path(info.path) / CORAL_HUB_DIR).exists()
    assert _git(info.path, "status", "--porcelain") == ""
    assert worktrees.dirty_paths(info.path) == []


def test_worktree_binding_off_preserves_existing_provisioning(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    root = _shared_root(tmp_path)
    worktrees = SubprocessSwarmWorktrees(
        var_root=tmp_path / "var" / "swarm",
        dep_link_dirs=(),
        coral_shared_root=root,
        coral_mode="off",
        lock_retry_sleep=0,
    )

    info = worktrees.create(str(repo), RUN_ID, "task-off", _git(repo, "rev-parse", "HEAD"))

    assert not (Path(info.path) / CORAL_HUB_DIR).exists()
    assert worktrees.dirty_paths(info.path) == []


def test_enforce_guard_is_actionable_when_shared_root_cannot_be_created(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocks mkdir", encoding="utf-8")
    coral_root = blocker / "coral"
    worktrees = SubprocessSwarmWorktrees(
        var_root=tmp_path / "var" / "swarm",
        dep_link_dirs=(),
        coral_shared_root=coral_root,
        coral_mode="enforce",
        lock_retry_sleep=0,
    )

    with pytest.raises(OSError) as excinfo:
        worktrees.create(str(repo), RUN_ID, "task-enforce", _git(repo, "rev-parse", "HEAD"))

    message = str(excinfo.value)
    assert str(coral_root.resolve()) in message
    assert "mkdir failed" in message
