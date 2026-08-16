"""Phase-2 worktree isolation — real-git suite on tmp repos.

Covers the ``SubprocessSwarmWorktrees`` contract end to end: create (fresh /
reuse-at-tip / registered-reuse / leftover-clearing / hook immunity /
dep-dir symlinks), remove + salvage (incl. stale index.lock tolerance),
merge (clean / noop / conflict with a pristine main), cumulative diffs,
orphan pruning, run-branch deletion, and the D4 config/env flag resolution.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import pytest

from omniagentos.swarm import worktrees as worktrees_module
from omniagentos.swarm.worktrees import (
    CORAL_KIND_LIMITS,
    DEFAULT_DEP_LINK_DIRS,
    SubprocessSwarmWorktrees,
    _ensure_coral_root,
    config_dep_link_dirs,
    swarm_worktrees_enabled,
)
from tests.swarm.scheduler_fakes import init_git_repo

RUN = "swr_test123"


def _git(cwd: Path | str, *args: str) -> str:
    proc = subprocess.run(
        ("git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _reflog(repo: Path | str) -> list[str]:
    """Main's reflog, newest first — the record that HEAD moved, however briefly."""

    return [line for line in _git(repo, "reflog", "--format=%H %gs").splitlines() if line]


def make_worktrees(tmp_path: Path, **overrides) -> tuple[SubprocessSwarmWorktrees, Path]:
    repo = tmp_path / "main"
    repo.mkdir(exist_ok=True)
    if not (repo / ".git").exists():
        init_git_repo(repo)
    kwargs = dict(
        var_root=tmp_path / "var" / "swarm",
        dep_link_dirs=(),
        lock_retry_attempts=2,
        lock_retry_sleep=0.01,
    )
    kwargs.update(overrides)
    return SubprocessSwarmWorktrees(**kwargs), repo


class TestCoralWorkspaceSetup:
    def test_enforce_creates_skeleton_and_empty_context_does_not_block_create(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        var_root = tmp_path / "var" / "swarm"
        coral_root = var_root / "coral"

        with caplog.at_level(logging.WARNING, logger="omniagentos.swarm.worktrees"):
            worktrees, repo = make_worktrees(tmp_path, coral_mode="enforce")

        assert not coral_root.exists()
        with caplog.at_level(logging.WARNING, logger="omniagentos.swarm.worktrees"):
            info = worktrees.create(str(repo), RUN, "task-coral", _git(repo, "rev-parse", "HEAD"))

        assert coral_root.is_dir()
        assert {path.name for path in coral_root.iterdir()} == set(CORAL_KIND_LIMITS)
        assert all((coral_root / kind).is_dir() for kind in CORAL_KIND_LIMITS)
        empty_warnings = [
            record.getMessage()
            for record in caplog.records
            if "workers will receive an empty CORAL context until the root is populated"
            in record.getMessage()
        ]
        assert empty_warnings == [
            f"CORAL enforce shared root {coral_root.resolve()} has no usable content; "
            "workers will receive an empty CORAL context until the root is populated"
        ]

        assert Path(info.path).is_dir()

    def test_off_does_not_create_coral_root(self, tmp_path: Path) -> None:
        var_root = tmp_path / "var" / "swarm"

        SubprocessSwarmWorktrees(
            var_root=var_root,
            dep_link_dirs=(),
            coral_mode="off",
            lock_retry_sleep=0,
        )

        assert not (var_root / "coral").exists()

    def test_coral_shared_root_symlink_is_canonicalized_without_construction_writes(
        self, tmp_path: Path
    ) -> None:
        canonical_root = tmp_path / "coral-target"
        canonical_root.mkdir()
        (canonical_root / "sentinel.md").write_text("keep", encoding="utf-8")
        configured_root = tmp_path / "coral-link"
        configured_root.symlink_to(canonical_root, target_is_directory=True)
        before = sorted(
            path.relative_to(canonical_root).as_posix() for path in canonical_root.rglob("*")
        )

        worktrees, _repo = make_worktrees(
            tmp_path,
            coral_shared_root=configured_root,
            coral_mode="off",
        )

        assert worktrees._coral_shared_root == canonical_root.resolve()
        assert configured_root.is_symlink()
        assert (
            sorted(
                path.relative_to(canonical_root).as_posix() for path in canonical_root.rglob("*")
            )
            == before
        )

    def test_shadow_unwritable_coral_root_warns_and_creates(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("permission-denied mkdir test is not meaningful as root")

        parent = tmp_path / "unwritable-coral-parent"
        parent.mkdir()
        coral_root = parent / "coral"
        os.chmod(parent, 0o500)
        try:
            worktrees, repo = make_worktrees(
                tmp_path,
                coral_shared_root=coral_root,
                coral_mode="shadow",
            )

            with caplog.at_level(logging.WARNING, logger="omniagentos.swarm.worktrees"):
                info = worktrees.create(
                    str(repo), RUN, "task-shadow-unwritable", _git(repo, "rev-parse", "HEAD")
                )

            assert Path(info.path).is_dir()
            assert any(
                f"Could not initialize CORAL shared root {coral_root}" in record.getMessage()
                and "configured mode's backstop" in record.getMessage()
                for record in caplog.records
            )
        finally:
            os.chmod(parent, 0o700)

    def test_enforce_unwritable_coral_root_raises(self, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("permission-denied mkdir test is not meaningful as root")

        parent = tmp_path / "unwritable-coral-parent"
        parent.mkdir()
        coral_root = parent / "coral"
        os.chmod(parent, 0o500)
        try:
            worktrees, repo = make_worktrees(
                tmp_path,
                coral_shared_root=coral_root,
                coral_mode="enforce",
            )

            with pytest.raises(OSError, match=r"mkdir failed.*coral"):
                worktrees.create(
                    str(repo), RUN, "task-enforce-unwritable", _git(repo, "rev-parse", "HEAD")
                )
        finally:
            os.chmod(parent, 0o700)

    def test_off_unwritable_coral_root_skips_ensure_and_creates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("permission-denied mkdir test is not meaningful as root")

        parent = tmp_path / "unwritable-coral-parent"
        parent.mkdir()
        coral_root = parent / "coral"
        os.chmod(parent, 0o500)
        calls: list[Path] = []

        def spy(root: Path) -> None:
            calls.append(root)
            _ensure_coral_root(root)

        monkeypatch.setattr(worktrees_module, "_ensure_coral_root", spy)
        try:
            worktrees, repo = make_worktrees(
                tmp_path,
                coral_shared_root=coral_root,
                coral_mode="off",
            )
            info = worktrees.create(
                str(repo), RUN, "task-off-unwritable", _git(repo, "rev-parse", "HEAD")
            )

            assert calls == []
            assert Path(info.path).is_dir()
        finally:
            os.chmod(parent, 0o700)

    def test_ensure_coral_root_is_idempotent(self, tmp_path: Path) -> None:
        coral_root = tmp_path / "var" / "swarm" / "coral"

        _ensure_coral_root(coral_root)
        _ensure_coral_root(coral_root)

        assert {path.name for path in coral_root.iterdir()} == set(CORAL_KIND_LIMITS)


class TestCreate:
    def test_fresh_create_places_worktree_on_task_branch_under_var_root(
        self, tmp_path: Path
    ) -> None:
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        assert info.path == str(tmp_path / "var" / "swarm" / "worktrees" / RUN / "taskA")
        assert info.branch == f"swarm/{RUN}/taskA"
        assert info.base_sha == base
        assert info.reused is False
        assert os.path.isdir(info.path)
        assert _git(info.path, "rev-parse", "--abbrev-ref", "HEAD") == info.branch
        # The worktree shares the main repo's object store.
        common = wt.git_common_dir(info.path)
        assert common == os.path.realpath(str(repo / ".git"))

    def test_successor_attempt_reuses_branch_at_tip(self, tmp_path: Path) -> None:
        """Committed partial work relays forward: remove the worktree, create
        again — the new worktree sits at the BRANCH tip, not the base."""
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        Path(info.path, "partial.txt").write_text("partial work", encoding="utf-8")
        _git(info.path, "add", "-A")
        _git(info.path, "commit", "-qm", "partial")
        tip = _git(info.path, "rev-parse", "HEAD")
        wt.remove(str(repo), info.path, salvage=False)

        again = wt.create(str(repo), RUN, "taskA", base)
        assert again.reused is True
        assert again.base_sha == tip
        assert Path(again.path, "partial.txt").read_text(encoding="utf-8") == "partial work"

    def test_registered_worktree_is_reused_as_is_with_uncommitted_work(
        self, tmp_path: Path
    ) -> None:
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        Path(info.path, "dirty.txt").write_text("uncommitted", encoding="utf-8")
        again = wt.create(str(repo), RUN, "taskA", base)
        assert again.reused is True
        assert again.path == info.path
        assert Path(again.path, "dirty.txt").read_text(encoding="utf-8") == "uncommitted"

    def test_unregistered_leftover_dir_is_cleared_before_fresh_add(self, tmp_path: Path) -> None:
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        leftover = wt.worktree_path(RUN, "taskA")
        leftover.mkdir(parents=True)
        (leftover / "junk.txt").write_text("crash leftover", encoding="utf-8")
        info = wt.create(str(repo), RUN, "taskA", base)
        assert not (Path(info.path) / "junk.txt").exists()
        assert _git(info.path, "rev-parse", "--abbrev-ref", "HEAD") == info.branch

    def test_failing_checkout_hook_does_not_break_create(self, tmp_path: Path) -> None:
        """Hooks are disabled during ``worktree add`` (verified live: a failing
        post-checkout hook fails the add outright without the disable)."""
        wt, repo = make_worktrees(tmp_path)
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        hook = hooks / "post-checkout"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        assert os.path.isdir(info.path)

    def test_dep_link_dirs_symlinked_when_present(self, tmp_path: Path) -> None:
        wt, repo = make_worktrees(tmp_path, dep_link_dirs=("node_modules", "vendor"))
        (repo / "node_modules").mkdir()
        (repo / "node_modules" / "left-pad.js").write_text("x", encoding="utf-8")
        # vendor absent in the main workspace -> no link.
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        link = Path(info.path) / "node_modules"
        assert link.is_symlink()
        assert (link / "left-pad.js").read_text(encoding="utf-8") == "x"
        assert not (Path(info.path) / "vendor").exists()

    def test_unsafe_components_are_rejected(self, tmp_path: Path) -> None:
        wt, repo = make_worktrees(tmp_path)
        with pytest.raises(ValueError):
            wt.create(str(repo), RUN, "../escape", "HEAD")


class TestRemoveAndSalvage:
    def test_salvage_commits_dirty_state_to_the_branch(self, tmp_path: Path) -> None:
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        Path(info.path, "wip.txt").write_text("reaped mid-flight", encoding="utf-8")
        sha = wt.remove(str(repo), info.path, salvage=True, message="salvage test")
        assert sha is not None
        assert not os.path.exists(info.path)
        # The salvage commit is on the branch: a fresh worktree resumes it.
        again = wt.create(str(repo), RUN, "taskA", base)
        assert Path(again.path, "wip.txt").read_text(encoding="utf-8") == "reaped mid-flight"
        assert _git(again.path, "log", "-1", "--format=%s") == "salvage test"

    def test_salvage_with_clean_tree_is_a_noop(self, tmp_path: Path) -> None:
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        outcome = wt.remove(str(repo), info.path, salvage=True)
        assert outcome.status == "removed"
        assert outcome.salvage_sha is None  # clean tree: nothing to salvage
        assert not os.path.exists(info.path)

    def test_reuse_clears_stale_index_lock(self, tmp_path: Path) -> None:
        """M2a: a reaped predecessor's stale ``index.lock`` is cleared
        (bounded retry) when a successor attempt REUSES the worktree —
        without it every git op of the successor fails."""
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        gitdir = Path(_git(info.path, "rev-parse", "--absolute-git-dir"))
        (gitdir / "index.lock").write_text("", encoding="utf-8")

        again = wt.create(str(repo), RUN, "taskA", base)
        assert again.reused is True
        assert not (gitdir / "index.lock").exists()
        # Git operations work again in the reused worktree.
        Path(again.path, "after.txt").write_text("ok", encoding="utf-8")
        _git(again.path, "add", "-A")
        _git(again.path, "commit", "-qm", "post-clear commit works")

    def test_dirty_paths_reports_only_unpersisted_state(self, tmp_path: Path) -> None:
        """M2d input: committed work is NOT dirty; uncommitted + untracked is."""
        wt, repo = make_worktrees(tmp_path)
        (repo / "tracked.txt").write_text("v1", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "seed")
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        Path(info.path, "committed.txt").write_text("done", encoding="utf-8")
        _git(info.path, "add", "-A")
        _git(info.path, "commit", "-qm", "committed")
        assert wt.dirty_paths(info.path) == []
        Path(info.path, "tracked.txt").write_text("v2", encoding="utf-8")
        Path(info.path, "untracked.txt").write_text("new", encoding="utf-8")
        assert wt.dirty_paths(info.path) == ["tracked.txt", "untracked.txt"]

    def test_salvage_commit_bypasses_failing_precommit_hook(self, tmp_path: Path) -> None:
        """m9: salvage add/commit carry the hooks disable themselves — a
        repo whose pre-commit hook fails must not silently skip salvage
        (the module docstring's bypass claim, now actually enforced)."""
        wt, repo = make_worktrees(tmp_path)
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in ("pre-commit", "post-checkout"):
            hook = hooks / name
            hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            hook.chmod(0o755)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        Path(info.path, "wip.txt").write_text("hooked repo", encoding="utf-8")
        sha = wt.remove(str(repo), info.path, salvage=True, message="salvage past hook")
        assert sha is not None
        assert _git(repo, "show", f"refs/heads/{info.branch}:wip.txt") == "hooked repo"

    def test_stale_index_lock_is_cleared_and_salvage_succeeds(self, tmp_path: Path) -> None:
        """R2c: a reaped worker can die mid-write leaving ``index.lock``.
        Salvage waits the bounded retry budget, then CLEARS the stale lock
        (every salvage caller runs only after the attempt's session is
        terminal — same precondition as create()'s reuse path) and commits.
        The partial work SURVIVES on the branch; removal proceeds."""
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        Path(info.path, "wip.txt").write_text("locked out", encoding="utf-8")
        gitdir = Path(_git(info.path, "rev-parse", "--absolute-git-dir"))
        (gitdir / "index.lock").write_text("", encoding="utf-8")
        outcome = wt.remove(str(repo), info.path, salvage=True)
        assert outcome.status == "removed"
        assert outcome.salvage_sha is not None  # lock cleared, work committed
        assert not os.path.exists(info.path)  # removal still happened
        # The salvaged file is reachable on the branch tip.
        shown = _git(repo, "show", f"{outcome.salvage_sha}:wip.txt")
        assert shown == "locked out"


class TestMerge:
    def _confirmed_worktree(self, wt, repo: Path, key: str, content: str):
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, key, base)
        Path(info.path, f"{key}.txt").write_text(content, encoding="utf-8")
        _git(info.path, "add", "-A")
        _git(info.path, "commit", "-qm", f"{key} work")
        return info

    def test_clean_merge_lands_no_ff_in_main(self, tmp_path: Path) -> None:
        wt, repo = make_worktrees(tmp_path)
        info = self._confirmed_worktree(wt, repo, "taskA", "delivered")
        outcome = wt.merge_branch(str(repo), info.branch, "merge taskA")
        assert outcome.status == "merged"
        assert outcome.sha == _git(repo, "rev-parse", "HEAD")
        assert (repo / "taskA.txt").read_text(encoding="utf-8") == "delivered"
        # --no-ff: a real merge commit with two parents.
        assert len(_git(repo, "rev-list", "--parents", "-1", "HEAD").split()) == 3

    def test_merge_of_unchanged_branch_is_noop(self, tmp_path: Path) -> None:
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        outcome = wt.merge_branch(str(repo), info.branch, "merge empty")
        assert outcome.status == "noop"
        assert _git(repo, "rev-parse", "HEAD") == base

    def test_conflict_captures_files_aborts_and_leaves_main_pristine(self, tmp_path: Path) -> None:
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        # Diverge: same file, both sides.
        (repo / "shared.txt").write_text("main side", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "main side")
        head = _git(repo, "rev-parse", "HEAD")
        Path(info.path, "shared.txt").write_text("worktree side", encoding="utf-8")
        _git(info.path, "add", "-A")
        _git(info.path, "commit", "-qm", "worktree side")

        outcome = wt.merge_branch(str(repo), info.branch, "merge conflict")
        assert outcome.status == "conflict"
        assert outcome.conflict_files == ("shared.txt",)
        # Aborted: HEAD unmoved, tree clean, no MERGE_HEAD.
        assert _git(repo, "rev-parse", "HEAD") == head
        assert _git(repo, "status", "--porcelain") == ""
        # The branch stays alive for integration's manual merge.
        assert _git(repo, "rev-parse", "--verify", f"refs/heads/{info.branch}")

    @pytest.mark.nsc_writer_proof("glue:fold-legs-pending@NSC-C08-04")
    def test_lane_work_reaches_main_only_through_the_merge_commit(self, tmp_path: Path) -> None:
        """NSC-C08-04's owed fold leg: the reflog audit for lane-authored commits.

        S3-merge.md: "TestMergeBack proves main stays pristine on conflict/abort;
        the reflog audit for lane-authored main commits is one new assertion in
        that class." Shared-checkout Git discipline is not only "main survives a
        conflict" — it is that a lane's commits can reach main ONLY as the second
        parent of a merge commit the coordinator made. A lane that committed
        directly onto the shared checkout would leave its commit in main's
        first-parent line and in main's reflog under its own name, and every
        existing assertion here would still pass.

        Exercises the real production seam (SubprocessSwarmWorktrees, the class
        omniagentos/swarm/scheduler.py constructs at :2920), not a stub.
        """
        worktrees = SubprocessSwarmWorktrees(
            var_root=tmp_path / "var" / "swarm",
            dep_link_dirs=(),
            lock_retry_attempts=2,
            lock_retry_sleep=0.01,
        )
        repo = tmp_path / "main"
        repo.mkdir(exist_ok=True)
        init_git_repo(repo)
        base = _git(repo, "rev-parse", "HEAD")
        info = worktrees.create(str(repo), RUN, "laneA", base)
        # Snapshot the WHOLE movement window before the lane does anything.
        # Counting only entries that carry the final merge sha is gameable: a
        # lane can commit straight onto main, `reset --hard` back, then merge,
        # and every graph-shaped assertion below still holds because the graph
        # was restored. The reflog is the only record that main MOVED, so the
        # audit has to be over the window, not over the surviving tip.
        reflog_before = _reflog(repo)
        tip_before = _git(repo, "rev-parse", "HEAD")
        Path(info.path, "laneA.txt").write_text("lane work", encoding="utf-8")
        _git(info.path, "add", "-A")
        _git(info.path, "commit", "-qm", "laneA work")
        lane_sha = _git(info.path, "rev-parse", "HEAD")

        outcome = worktrees.merge_branch(str(repo), info.branch, "merge laneA")

        assert outcome.status == "merged"
        merge_sha = _git(repo, "rev-parse", "HEAD")
        parents = _git(repo, "rev-list", "--parents", "-1", "HEAD").split()
        assert parents[1:] == [base, lane_sha], parents
        # THE AUDIT: the lane commit is reachable from main, but never on its
        # first-parent line -- it arrived as the merge's second parent.
        assert lane_sha in _git(repo, "rev-list", "HEAD").split()
        assert lane_sha not in _git(repo, "rev-list", "--first-parent", "HEAD").split()
        # main did not move at all while the lane worked...
        assert tip_before == base, (tip_before, base)
        # ...and the merge is the ONLY movement main's reflog gained.
        reflog_after = _reflog(repo)
        new_entries = reflog_after[: len(reflog_after) - len(reflog_before)]
        assert len(new_entries) == 1, reflog_after
        assert new_entries[0].startswith(merge_sha), new_entries
        assert "merge" in new_entries[0].lower(), new_entries
        assert not any(line.startswith(lane_sha) for line in reflog_after), reflog_after

    def test_a_direct_main_commit_reset_away_before_the_merge_is_still_caught(
        self, tmp_path: Path
    ) -> None:
        """The bypass the graph-shaped assertions cannot see.

        A lane commits straight onto the shared checkout, `reset --hard` back to
        base, then merges properly. Afterwards the DAG is indistinguishable from
        a disciplined run: merge commit with the right two parents, lane work off
        the first-parent line, no surviving lane entry at the tip. Only the
        movement WINDOW records that main moved three times instead of once.
        """
        worktrees, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        info = worktrees.create(str(repo), RUN, "laneA", base)
        reflog_before = _reflog(repo)
        Path(info.path, "laneA.txt").write_text("lane work", encoding="utf-8")
        _git(info.path, "add", "-A")
        _git(info.path, "commit", "-qm", "laneA work")
        lane_sha = _git(info.path, "rev-parse", "HEAD")

        # the violation, then the cover-up
        (repo / "direct.txt").write_text("lane wrote straight to main", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "LANE DIRECTLY ON MAIN")
        _git(repo, "reset", "--hard", base)

        outcome = worktrees.merge_branch(str(repo), info.branch, "merge laneA")
        assert outcome.status == "merged"

        # every graph assertion the audit makes still holds...
        merge_sha = _git(repo, "rev-parse", "HEAD")
        assert _git(repo, "rev-list", "--parents", "-1", "HEAD").split()[1:] == [base, lane_sha]
        assert lane_sha not in _git(repo, "rev-list", "--first-parent", "HEAD").split()
        reflog_after = _reflog(repo)
        assert len([line for line in reflog_after if line.startswith(merge_sha)]) == 1
        # ...and the window audit is what refuses it.
        new_entries = reflog_after[: len(reflog_after) - len(reflog_before)]
        assert len(new_entries) == 3, new_entries
        assert any("LANE DIRECTLY ON MAIN" in line for line in new_entries), new_entries

    def test_merge_exception_path_aborts_wedged_merge(self, tmp_path: Path) -> None:
        """M4a: a merge that dies mid-flight (TimeoutExpired — not just
        returncode!=0) still aborts best-effort, so the MAIN workspace never
        stays wedged with MERGE_HEAD."""

        class ExplodingMerge(SubprocessSwarmWorktrees):
            def _git(self, cwd, *args, check=True):  # type: ignore[override]
                proc = super()._git(cwd, *args, check=check)
                if "merge" in args and "-m" in args:
                    # Simulate the subprocess dying AFTER git already started
                    # the merge (the conflicted merge left MERGE_HEAD behind).
                    raise subprocess.TimeoutExpired(cmd=list(args), timeout=120)
                return proc

        wt = ExplodingMerge(
            var_root=tmp_path / "var" / "swarm",
            dep_link_dirs=(),
            lock_retry_attempts=2,
            lock_retry_sleep=0.01,
        )
        repo = tmp_path / "main"
        repo.mkdir()
        init_git_repo(repo)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        # Force a genuine conflict so the merge really leaves MERGE_HEAD.
        (repo / "shared.txt").write_text("main side", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "main side")
        Path(info.path, "shared.txt").write_text("worktree side", encoding="utf-8")
        _git(info.path, "add", "-A")
        _git(info.path, "commit", "-qm", "worktree side")

        with pytest.raises(subprocess.TimeoutExpired):
            wt.merge_branch(str(repo), info.branch, "merge that dies")
        # Aborted on the exception path: no MERGE_HEAD, clean tree.
        merge_head = subprocess.run(
            ("git", "-C", str(repo), "rev-parse", "-q", "--verify", "MERGE_HEAD"),
            capture_output=True,
            text=True,
        )
        assert merge_head.returncode != 0
        assert _git(repo, "status", "--porcelain") == ""

    def test_has_pending_merge_and_abort_merge(self, tmp_path: Path) -> None:
        """M4c primitives: MERGE_HEAD detection + best-effort abort."""
        wt, repo = make_worktrees(tmp_path)
        assert wt.has_pending_merge(str(repo)) is False
        assert wt.abort_merge(str(repo)) is False  # nothing to abort
        # Wedge the workspace: a real conflicted merge WITHOUT the abort.
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        (repo / "shared.txt").write_text("main side", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "main side")
        Path(info.path, "shared.txt").write_text("worktree side", encoding="utf-8")
        _git(info.path, "add", "-A")
        _git(info.path, "commit", "-qm", "worktree side")
        subprocess.run(
            # Explicit ident (same flags as _git): without it the merge dies at
            # ident resolution on hosts with no git identity and never writes
            # MERGE_HEAD, so has_pending_merge sees nothing.
            (
                "git",
                "-C",
                str(repo),
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "merge",
                "--no-ff",
                "-m",
                "wedge",
                info.branch,
            ),
            capture_output=True,
            text=True,
        )
        assert wt.has_pending_merge(str(repo)) is True
        assert wt.abort_merge(str(repo)) is True
        assert wt.has_pending_merge(str(repo)) is False
        assert _git(repo, "status", "--porcelain") == ""

    def test_merge_by_sha_ignores_a_tampered_branch_ref(self, tmp_path: Path) -> None:
        """M3: the coordinator merges the exact quality-gate-verified SHA —
        a branch ref moved to a different commit after the gate (same-run
        sibling tampering through the shared .git) changes NOTHING about
        what lands in main."""
        wt, repo = make_worktrees(tmp_path)
        info = self._confirmed_worktree(wt, repo, "taskA", "verified content")
        verified_sha = wt.head_sha(info.path)
        assert verified_sha == _git(info.path, "rev-parse", "HEAD")

        # Tamper: move the task branch to a rogue commit (evil.txt).
        rogue = wt.create(str(repo), RUN, "rogue", _git(repo, "rev-parse", "HEAD"))
        Path(rogue.path, "evil.txt").write_text("tampered", encoding="utf-8")
        _git(rogue.path, "add", "-A")
        _git(rogue.path, "commit", "-qm", "evil")
        rogue_sha = _git(rogue.path, "rev-parse", "HEAD")
        wt.remove(str(repo), info.path, salvage=False)
        _git(repo, "update-ref", f"refs/heads/{info.branch}", rogue_sha)

        outcome = wt.merge_branch(str(repo), info.branch, "merge taskA", sha=verified_sha)
        assert outcome.status == "merged"
        # The VERIFIED work landed; the tampered ref's content did not.
        assert (repo / "taskA.txt").read_text(encoding="utf-8") == "verified content"
        assert not (repo / "evil.txt").exists()


class TestChangedPaths:
    def test_committed_uncommitted_and_untracked_are_all_reported(self, tmp_path: Path) -> None:
        wt, repo = make_worktrees(tmp_path, dep_link_dirs=("node_modules",))
        (repo / "node_modules").mkdir()
        (repo / "tracked.txt").write_text("v1", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "seed")
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)

        Path(info.path, "committed.txt").write_text("done", encoding="utf-8")
        _git(info.path, "add", "-A")
        _git(info.path, "commit", "-qm", "committed work")
        Path(info.path, "tracked.txt").write_text("v2", encoding="utf-8")  # uncommitted
        Path(info.path, "untracked.txt").write_text("new", encoding="utf-8")

        changed = wt.changed_paths_since(info.path, base)
        assert changed == ["committed.txt", "tracked.txt", "untracked.txt"]
        # The symlink-shared dep dir never counts as task output.
        assert "node_modules" not in changed


class TestGcAndBranches:
    def test_prune_orphans_removes_only_non_live_worktrees(self, tmp_path: Path) -> None:
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        live = wt.create(str(repo), RUN, "live", base)
        dead = wt.create(str(repo), RUN, "dead", base)
        Path(dead.path, "partial.txt").write_text("salvage me", encoding="utf-8")

        removed = wt.prune_orphans(str(repo), RUN, ["live"])
        assert removed == [os.path.realpath(dead.path)] or removed == [dead.path]
        assert os.path.isdir(live.path)
        assert not os.path.exists(dead.path)
        # Orphan removal salvaged the partial work onto the branch.
        assert _git(repo, "show", f"refs/heads/{dead.branch}:partial.txt") == "salvage me"

    def test_list_run_worktrees_is_scoped_to_the_run(self, tmp_path: Path) -> None:
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        wt.create(str(repo), RUN, "a", base)
        wt.create(str(repo), "swr_other", "b", base)
        listed = wt.list_run_worktrees(str(repo), RUN)
        assert [key for _, key in listed] == ["a"]

    def test_delete_run_branches_scoped_and_after_worktree_removal(self, tmp_path: Path) -> None:
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "a", base)
        other = wt.create(str(repo), "swr_other", "b", base)
        wt.remove(str(repo), info.path, salvage=False)
        deleted = wt.delete_run_branches(str(repo), RUN)
        assert deleted == [f"swarm/{RUN}/a"]
        # The other run's branch survives.
        assert _git(repo, "rev-parse", "--verify", f"refs/heads/{other.branch}")

    def test_checked_out_branch_is_not_deleted(self, tmp_path: Path) -> None:
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        wt.create(str(repo), RUN, "a", base)  # worktree still holds the branch
        assert wt.delete_run_branches(str(repo), RUN) == []

    def test_unmerged_branch_survives_delete_run_branches(self, tmp_path: Path) -> None:
        """M2: terminal cleanup must never force-delete a branch that still
        carries unmerged (conflict-routed or salvaged) work."""
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "a", base)
        Path(info.path, "unmerged.txt").write_text("salvaged work", encoding="utf-8")
        wt.remove(str(repo), info.path, salvage=True, message="salvage")
        assert wt.delete_run_branches(str(repo), RUN) == []
        assert _git(repo, "show", f"refs/heads/{info.branch}:unmerged.txt") == "salvaged work"


class TestProbes:
    def test_supported_true_on_repo_false_on_plain_dir(self, tmp_path: Path) -> None:
        wt, repo = make_worktrees(tmp_path)
        assert wt.supported(str(repo)) is True
        plain = tmp_path / "plain"
        plain.mkdir()
        assert wt.supported(str(plain)) is False

    def test_git_common_dir_is_absolute_and_shared_across_worktrees(self, tmp_path: Path) -> None:
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "a", base)
        expected = os.path.realpath(str(repo / ".git"))
        assert wt.git_common_dir(str(repo)) == expected
        assert wt.git_common_dir(info.path) == expected
        assert wt.git_common_dir(str(tmp_path / "nope")) is None


class TestFlagResolution:
    def test_env_override_wins_both_directions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "omniagentos.swarm.worktrees.worktrees_config", lambda: {"enabled": False}
        )
        monkeypatch.setenv("OMNIAGENTOS_SWARM_WORKTREES", "1")
        assert swarm_worktrees_enabled() is True
        monkeypatch.setattr(
            "omniagentos.swarm.worktrees.worktrees_config", lambda: {"enabled": True}
        )
        monkeypatch.setenv("OMNIAGENTOS_SWARM_WORKTREES", "0")
        assert swarm_worktrees_enabled() is False

    def test_config_flag_used_when_env_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """H-35 re-contract: config ``enabled: true`` alone does NOT enable
        production isolation until L11 fencing is ready. Explicit false still
        forces off. Drill force-on remains the env path
        (``test_env_override_wins_both_directions``).

        Pre-H-35 this asserted config true → enabled. That failed open for
        collision safety. Safety is deliberately stronger now: config true +
        closed L11 gate stays off; config true + open gate enables.
        """
        monkeypatch.delenv("OMNIAGENTOS_SWARM_WORKTREES", raising=False)
        monkeypatch.delenv("OMNIAGENTOS_L11_FENCING_READY", raising=False)
        monkeypatch.delenv("OMNIAGENTOS_COLLISION_SAFETY", raising=False)
        # Gate closed: config true must stay dark (H-35). The shipped
        # parallelism config opened the gate on 2026-07-26, so the closed-gate
        # phase pins its own config file rather than relying on the checked-in
        # default.
        closed_cfg = tmp_path / "parallelism-closed.yaml"
        closed_cfg.write_text(
            "collision_safety:\n"
            "  mode: shadow\n"
            "  fencing_ready: false\n"
            "  allow_enforce: false\n"
            "  require_shadow_evidence: false\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(closed_cfg))
        monkeypatch.setattr(
            "omniagentos.swarm.worktrees.worktrees_config", lambda: {"enabled": True}
        )
        assert swarm_worktrees_enabled() is False
        monkeypatch.setattr(
            "omniagentos.swarm.worktrees.worktrees_config", lambda: {"enabled": False}
        )
        assert swarm_worktrees_enabled() is False
        # Gate open: config true becomes effective isolation.
        monkeypatch.setattr(
            "omniagentos.swarm.scope_wiring.commit_fence_contract",
            lambda: {"version": 1, "capabilities": ("commit_guard", "commit_claim")},
            raising=False,
        )
        cfg = tmp_path / "parallelism.yaml"
        cfg.write_text(
            "collision_safety:\n"
            "  mode: shadow\n"
            "  fencing_ready: true\n"
            "  allow_enforce: true\n"
            "  require_shadow_evidence: false\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(cfg))
        monkeypatch.setattr(
            "omniagentos.swarm.worktrees.worktrees_config", lambda: {"enabled": True}
        )
        assert swarm_worktrees_enabled() is True

    def test_shipped_config_defaults_on_with_venv_free_link_dirs(self) -> None:
        # 2026-07-26 operator decision: L11 fencing landed, the gate opened,
        # and the checked-in default is now enabled (supersedes the D4 opt-in
        # pin). .venv/venv must still never be symlink-shared (absolute paths
        # do not survive relocation).
        from omniagentos.swarm.worktrees import worktrees_config

        config = worktrees_config()
        assert bool(config.get("enabled")) is True
        assert ".venv" not in DEFAULT_DEP_LINK_DIRS
        assert "venv" not in DEFAULT_DEP_LINK_DIRS
        for name in config.get("dep_link_dirs") or []:
            assert name not in (".venv", "venv")

    def test_install_per_worktree_disables_symlink_sharing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "omniagentos.swarm.worktrees.worktrees_config",
            lambda: {"install_per_worktree": True, "dep_link_dirs": ["node_modules"]},
        )
        assert config_dep_link_dirs() == ()


class TestRemoveKeepsUnsalvageableWork:
    def test_salvage_failure_on_dirty_tree_leaves_worktree_in_place(
        self, tmp_path, monkeypatch
    ) -> None:
        """R2b: remove(salvage=True) must NEVER rmtree a tree that is still
        dirty after a failed salvage — the unpersisted work is the only copy
        (never committed, not even reflog-recoverable)."""
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        Path(info.path, "wip.txt").write_text("only copy", encoding="utf-8")
        monkeypatch.setattr(type(wt), "salvage_commit", lambda self, path, message: None)
        outcome = wt.remove(str(repo), info.path, salvage=True)
        assert outcome.status == "salvage_failed"
        assert Path(info.path, "wip.txt").read_text(encoding="utf-8") == "only copy"

    def test_prune_orphans_skips_salvage_failed_worktrees(self, tmp_path, monkeypatch) -> None:
        """R2: crash-resume orphan GC must not destroy a dirty worktree whose
        salvage fails — it stays on disk and is NOT reported removed."""
        wt, repo = make_worktrees(tmp_path)
        base = _git(repo, "rev-parse", "HEAD")
        info = wt.create(str(repo), RUN, "taskA", base)
        Path(info.path, "wip.txt").write_text("only copy", encoding="utf-8")
        monkeypatch.setattr(type(wt), "salvage_commit", lambda self, path, message: None)
        removed = wt.prune_orphans(str(repo), RUN, live_task_keys=[])
        assert removed == []
        assert Path(info.path, "wip.txt").exists()
