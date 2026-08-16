"""AT-07 — Execution: work runs, worktrees isolate, artifacts land, status flows.

Grounded in the real execution plumbing:

* ``omniagentos.worktrees.git.SubprocessWorktrees`` — create / merge / salvage /
  prune, driven against a REAL git repository under ``tmp_path``. No fake git
  seam is used for the isolation proofs, because the claim being tested ("a
  worker cannot see or clobber a sibling's tree") is a property of git itself.
* ``omniagentos.swarm.spawn`` — ``write_task_md`` / ``init_swarm_workbook``,
  which produce ``var/swarm/<run>/<task>/{TASK.md,WORKBOOK.md}``.
* the scheduler's emitter seam — the lifecycle actions declared in
  ``omniagentos.swarm.contracts``.

Hermetic: every git repository, worktree and var root lives under ``tmp_path``;
git runs with a pinned identity and hooks disabled (``SubprocessWorktrees`` does
that itself), so nothing touches the operator checkout or the network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from omniagentos.swarm.contracts import (
    ACTION_RUN_COMPLETED,
    ACTION_RUN_STARTED,
    ACTION_TASK_ASSIGNED,
    ACTION_TASK_COMPLETED,
)
from omniagentos.swarm.spawn import (
    init_swarm_workbook,
    swarm_workbook_path,
    write_task_md,
)
from omniagentos.worktrees.git import (
    MergeOutcome,
    RemoveOutcome,
    SubprocessWorktrees,
    WorktreeInfo,
)
from tests.swarm.scheduler_fakes import make_harness, make_scheduler

OWNER = "run_at07"


@pytest.fixture(autouse=True)
def _pin_default_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


def git(cwd: Path | str, *args: str) -> str:
    proc = subprocess.run(
        ("git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A real, isolated git repository with one commit."""
    path = tmp_path / "repo"
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(("git", "init", "-q", "-b", "main", str(path)), check=True, capture_output=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


@pytest.fixture()
def worktrees(tmp_path: Path) -> SubprocessWorktrees:
    return SubprocessWorktrees(namespace="at07", var_root=tmp_path / "var")


def commit_in(path: str, name: str, content: str) -> str:
    Path(path, name).write_text(content, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", f"add {name}")
    return git(path, "rev-parse", "HEAD")


# ==========================================================================
# Worktrees are isolated
# ==========================================================================


class TestWorktreeIsolation:
    def test_two_units_get_distinct_trees_and_branches(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        assert worktrees.supported(str(repo)), "git worktrees unsupported; the suite is vacuous"

        a = worktrees.create(str(repo), OWNER, "unit-a", "HEAD")
        b = worktrees.create(str(repo), OWNER, "unit-b", "HEAD")

        assert isinstance(a, WorktreeInfo) and isinstance(b, WorktreeInfo)
        assert a.path != b.path
        assert a.branch != b.branch
        assert Path(a.path).is_dir() and Path(b.path).is_dir()
        assert a.base_sha == b.base_sha, "both units must fork from the same base"

    def test_a_units_work_is_invisible_to_its_sibling_and_to_main(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        """The core isolation claim. If this passes with the worktree seam
        replaced by a shared directory, the whole model is broken."""
        a = worktrees.create(str(repo), OWNER, "unit-a", "HEAD")
        b = worktrees.create(str(repo), OWNER, "unit-b", "HEAD")

        commit_in(a.path, "only-in-a.txt", "written by unit-a\n")

        assert Path(a.path, "only-in-a.txt").exists()
        assert not Path(b.path, "only-in-a.txt").exists(), "unit-b saw unit-a's file"
        assert not (repo / "only-in-a.txt").exists(), "the main workspace saw unit-a's file"

    def test_units_editing_the_same_file_do_not_clobber_each_other(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        a = worktrees.create(str(repo), OWNER, "unit-a", "HEAD")
        b = worktrees.create(str(repo), OWNER, "unit-b", "HEAD")

        commit_in(a.path, "README.md", "rewritten by A\n")
        commit_in(b.path, "README.md", "rewritten by B\n")

        assert Path(a.path, "README.md").read_text(encoding="utf-8") == "rewritten by A\n"
        assert Path(b.path, "README.md").read_text(encoding="utf-8") == "rewritten by B\n"
        assert (repo / "README.md").read_text(encoding="utf-8") == "base\n"

    def test_reuse_preserves_uncommitted_partial_work(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        """A successor attempt inherits its predecessor's in-progress tree — the
        relay behaviour ``create`` documents."""
        first = worktrees.create(str(repo), OWNER, "unit-a", "HEAD")
        Path(first.path, "partial.txt").write_text("half done\n", encoding="utf-8")

        second = worktrees.create(str(repo), OWNER, "unit-a", "HEAD")

        assert second.reused is True
        assert second.path == first.path
        assert Path(second.path, "partial.txt").read_text(encoding="utf-8") == "half done\n"


# ==========================================================================
# Artifacts are produced
# ==========================================================================


class TestArtifactsAreProduced:
    def test_changed_paths_reports_committed_and_uncommitted_work(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        info = worktrees.create(str(repo), OWNER, "unit-a", "HEAD")
        commit_in(info.path, "committed.txt", "done\n")
        Path(info.path, "uncommitted.txt").write_text("wip\n", encoding="utf-8")

        changed = worktrees.changed_paths_since(info.path, info.base_sha)

        assert "committed.txt" in changed, "a committed artifact was not reported"
        assert "uncommitted.txt" in changed, "an untracked artifact was not reported"

    def test_dirty_paths_reports_only_the_unpersisted_slice(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        info = worktrees.create(str(repo), OWNER, "unit-a", "HEAD")
        commit_in(info.path, "committed.txt", "done\n")
        Path(info.path, "uncommitted.txt").write_text("wip\n", encoding="utf-8")

        dirty = worktrees.dirty_paths(info.path)

        assert dirty == ["uncommitted.txt"], f"dirty_paths misreported: {dirty}"

    def test_clean_worktree_reports_no_artifacts(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        """Positive control: the reporters are not simply listing every file."""
        info = worktrees.create(str(repo), OWNER, "unit-a", "HEAD")

        assert worktrees.dirty_paths(info.path) == []
        assert worktrees.changed_paths_since(info.path, info.base_sha) == []

    def test_task_md_and_workbook_land_under_the_run_task_dir(self, tmp_path: Path) -> None:
        """``var/swarm/<run>/<task>/{TASK.md,WORKBOOK.md}`` is the durable
        execution contract + continuity record."""
        var_root = tmp_path / "var" / "swarm"
        workbook = swarm_workbook_path("run-7", "task-1", root=var_root)
        assert workbook.parts[-3:] == ("run-7", "task-1", "WORKBOOK.md")

        init_swarm_workbook(workbook, "Unit A", "ship the thing", "suite green")
        task_md = write_task_md(
            workbook.parent,
            {"id": "run-7", "goal": "ship the thing"},
            {"id": "task-1", "title": "Unit A", "description": "do the work"},
        )

        assert task_md.exists() and task_md.name == "TASK.md"
        assert task_md.parent == workbook.parent
        workbook_text = workbook.read_text(encoding="utf-8")
        assert "ship the thing" in workbook_text
        assert "suite green" in workbook_text
        assert "Unit A" in task_md.read_text(encoding="utf-8")

    def test_existing_artifacts_are_never_overwritten(self, tmp_path: Path) -> None:
        """Both files are continuity records: a re-attempt must not erase the
        predecessor's log."""
        var_root = tmp_path / "var" / "swarm"
        workbook = swarm_workbook_path("run-7", "task-1", root=var_root)
        init_swarm_workbook(workbook, "Unit A", "goal", "criteria")
        workbook.write_text("## Progress log\n\nattempt 1 notes\n", encoding="utf-8")
        task_md = write_task_md(
            workbook.parent, {"id": "run-7"}, {"id": "task-1", "title": "Unit A"}
        )
        task_md.write_text("pinned contract\n", encoding="utf-8")

        init_swarm_workbook(workbook, "Unit A", "goal", "criteria")
        write_task_md(workbook.parent, {"id": "run-7"}, {"id": "task-1", "title": "Unit A"})

        assert "attempt 1 notes" in workbook.read_text(encoding="utf-8")
        assert task_md.read_text(encoding="utf-8") == "pinned contract\n"


# ==========================================================================
# Merge: assigned work reaches the main workspace
# ==========================================================================


class TestMergeIntegratesWork:
    def test_unit_work_merges_into_the_main_workspace(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        info = worktrees.create(str(repo), OWNER, "unit-a", "HEAD")
        commit_in(info.path, "delivered.txt", "unit output\n")

        outcome = worktrees.merge_branch(str(repo), info.branch, "merge unit-a")

        assert isinstance(outcome, MergeOutcome)
        assert outcome.status == "merged", f"merge failed: {outcome.detail}"
        assert (repo / "delivered.txt").read_text(encoding="utf-8") == "unit output\n"

    def test_merging_nothing_is_a_noop_not_a_false_success(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        info = worktrees.create(str(repo), OWNER, "unit-a", "HEAD")

        outcome = worktrees.merge_branch(str(repo), info.branch, "merge empty unit-a")

        assert outcome.status == "noop", "an empty branch reported real work merged"

    def test_merge_pins_the_verified_sha_not_the_branch_ref(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        """Ref-tampering closure: the merge target is the sha the quality gate
        verified. Work pushed onto the branch afterwards must NOT land."""
        info = worktrees.create(str(repo), OWNER, "unit-a", "HEAD")
        verified_sha = commit_in(info.path, "verified.txt", "reviewed\n")
        commit_in(info.path, "smuggled.txt", "never reviewed\n")

        outcome = worktrees.merge_branch(str(repo), info.branch, "merge unit-a", sha=verified_sha)

        assert outcome.status == "merged"
        assert (repo / "verified.txt").exists()
        assert not (repo / "smuggled.txt").exists(), "post-verification work was merged"

    def test_conflict_is_reported_and_the_workspace_left_pristine(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        a = worktrees.create(str(repo), OWNER, "unit-a", "HEAD")
        b = worktrees.create(str(repo), OWNER, "unit-b", "HEAD")
        commit_in(a.path, "README.md", "A wins\n")
        commit_in(b.path, "README.md", "B wins\n")
        assert worktrees.merge_branch(str(repo), a.branch, "merge a").status == "merged"

        outcome = worktrees.merge_branch(str(repo), b.branch, "merge b")

        assert outcome.status == "conflict", "a conflicting merge reported success"
        assert "README.md" in outcome.conflict_files
        assert not worktrees.has_pending_merge(str(repo)), "the workspace was left wedged"
        assert (repo / "README.md").read_text(encoding="utf-8") == "A wins\n"


# ==========================================================================
# Salvage / prune: partial work survives, orphans are reclaimed
# ==========================================================================


class TestSalvageAndPrune:
    def test_salvage_persists_partial_work_before_removal(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        info = worktrees.create(str(repo), OWNER, "unit-a", "HEAD")
        Path(info.path, "partial.txt").write_text("half a feature\n", encoding="utf-8")

        outcome = worktrees.remove(str(repo), info.path, salvage=True, message="salvage a")

        assert isinstance(outcome, RemoveOutcome)
        assert outcome.status == "removed"
        assert outcome.salvage_sha, "dirty work was discarded without a salvage commit"
        assert not Path(info.path).exists()
        blob = git(repo, "show", f"{outcome.salvage_sha}:partial.txt")
        assert blob == "half a feature", "the salvage commit does not contain the partial work"

    def test_removal_without_salvage_discards_the_tree(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        """Positive control for the salvage assertion above."""
        info = worktrees.create(str(repo), OWNER, "unit-a", "HEAD")
        Path(info.path, "partial.txt").write_text("half a feature\n", encoding="utf-8")

        outcome = worktrees.remove(str(repo), info.path, salvage=False)

        assert outcome.status == "removed"
        assert outcome.salvage_sha is None
        assert not Path(info.path).exists()

    def test_salvage_on_a_clean_tree_records_no_commit(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        info = worktrees.create(str(repo), OWNER, "unit-a", "HEAD")

        outcome = worktrees.remove(str(repo), info.path, salvage=True)

        assert outcome.status == "removed"
        assert outcome.salvage_sha is None, "an empty salvage commit was created"

    def test_prune_reclaims_orphans_and_keeps_live_units(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        live = worktrees.create(str(repo), OWNER, "unit-live", "HEAD")
        orphan = worktrees.create(str(repo), OWNER, "unit-orphan", "HEAD")

        removed = worktrees.prune_orphans(str(repo), OWNER, live_task_keys=["unit-live"])

        assert Path(orphan.path).resolve() in {Path(p).resolve() for p in removed}
        assert not Path(orphan.path).exists(), "an orphaned worktree was not reclaimed"
        assert Path(live.path).exists(), "a LIVE worktree was destroyed by the pruner"

    def test_terminal_cleanup_keeps_branches_that_still_carry_work(
        self, repo: Path, worktrees: SubprocessWorktrees
    ) -> None:
        """``delete_run_branches`` uses ``branch -d``, never ``-D``: a branch
        holding unmerged (salvaged or conflicted) work must survive, or the
        salvage that preserved it was theater."""
        merged = worktrees.create(str(repo), OWNER, "unit-merged", "HEAD")
        unmerged = worktrees.create(str(repo), OWNER, "unit-unmerged", "HEAD")
        commit_in(merged.path, "merged.txt", "landed\n")
        commit_in(unmerged.path, "orphaned.txt", "never landed\n")
        assert worktrees.merge_branch(str(repo), merged.branch, "merge").status == "merged"
        worktrees.remove(str(repo), merged.path, salvage=False)
        worktrees.remove(str(repo), unmerged.path, salvage=False)

        deleted = worktrees.delete_run_branches(str(repo), OWNER)

        assert merged.branch in deleted
        assert unmerged.branch not in deleted, "a branch with unmerged work was deleted"
        surviving = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
        assert unmerged.branch in surviving.splitlines()


# ==========================================================================
# Status updates flow
# ==========================================================================


class TestStatusUpdatesFlow:
    def test_lifecycle_events_are_emitted_in_order(self, tmp_path: Path) -> None:
        """Every executed unit must be observable: assignment, completion, and
        the run's own start/finish, in that order."""
        h = make_harness(tmp_path, [{"id": "a"}, {"id": "b"}], max_concurrency=2, integration=False)
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=60)

            actions = h.emitter.actions()
            assert ACTION_RUN_STARTED in actions
            assert ACTION_RUN_COMPLETED in actions
            assert actions.index(ACTION_RUN_STARTED) < actions.index(ACTION_TASK_ASSIGNED)
            assert actions.index(ACTION_TASK_COMPLETED) < actions.index(ACTION_RUN_COMPLETED)

            assigned = {p.get("task_id") for p in h.emitter.of(ACTION_TASK_ASSIGNED)}
            completed = {p.get("task_id") for p in h.emitter.of(ACTION_TASK_COMPLETED)}
            expected = {h.task_id("a"), h.task_id("b")}
            assert expected <= assigned, "a scheduled unit was never announced as assigned"
            assert expected <= completed, "a finished unit never reported completion"
        finally:
            h.close()

    def test_each_unit_actually_executes_its_assigned_work(self, tmp_path: Path) -> None:
        """Assignment is not enough: the spawner must be handed every unit, and
        the durable board must record the result."""
        h = make_harness(
            tmp_path,
            [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            max_concurrency=3,
            integration=False,
        )
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=60)

            assert set(h.world.spawn_order) == {"a", "b", "c"}, (
                f"not every unit was executed: {h.world.spawn_order}"
            )
            for key in ("a", "b", "c"):
                assert h.status_of(key) == "done"
                attempts = h.attempts_of(key)
                assert attempts, f"{key} has no recorded attempt"
                assert attempts[-1]["end_reason"] == "completed"
        finally:
            h.close()

    def test_each_spawn_is_scoped_to_its_own_working_dir(self, tmp_path: Path) -> None:
        """A status update is only meaningful if the work it describes was
        actually confined to that unit's workspace."""
        h = make_harness(tmp_path, [{"id": "a"}, {"id": "b"}], max_concurrency=2, integration=False)
        try:
            scheduler = make_scheduler(h)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=60)

            assert h.world.spawn_requests
            for request in h.world.spawn_requests:
                working_dir = str(getattr(request, "working_dir", "") or "")
                assert working_dir, f"spawn for {request.task_key} had no working_dir"
                assert Path(working_dir).resolve().is_relative_to(tmp_path.resolve()), (
                    f"{request.task_key} executed outside the test sandbox: {working_dir}"
                )
        finally:
            h.close()


# ==========================================================================
# Sandbox confinement of the execution surface
# ==========================================================================


class TestSandboxConfinement:
    def test_wrap_command_is_a_no_op_without_a_workspace(self) -> None:
        """``wrap_command`` returns argv UNCHANGED when it cannot confine it.

        That is a documented fallback, and it is exactly why callers needing real
        egress confinement must consult ``wrap_available`` first — pinned here so
        the contract cannot drift silently.
        """
        from omniagentos.runner.sandbox import wrap_available, wrap_command

        argv = ["echo", "hello"]
        assert wrap_command(argv, None) == argv
        assert wrap_available(argv, None) is False
        assert wrap_available([], "/tmp") is False

    def test_wrap_command_confines_writes_to_the_workspace(self, tmp_path: Path) -> None:
        """When the OS sandbox is available the argv is wrapped and the workspace
        appears in the profile. Skipped (not faked) where Seatbelt is absent."""
        from omniagentos.runner.sandbox import wrap_available, wrap_command

        argv = ["echo", "hello"]
        if not wrap_available(argv, str(tmp_path)):
            pytest.skip("OS sandbox (Seatbelt) unavailable on this host")

        wrapped = wrap_command(argv, str(tmp_path))

        assert wrapped != argv, "the sandbox was available but argv was not wrapped"
        assert wrapped[-len(argv) :] == argv
        assert "-p" in wrapped
        profile = wrapped[wrapped.index("-p") + 1]
        assert str(Path(tmp_path).resolve()) in profile
        assert "deny" in profile, "the profile denies nothing"


def _unused(*_: Any) -> None:  # pragma: no cover - keeps Any import meaningful
    return None
