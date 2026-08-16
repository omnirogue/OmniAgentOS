"""scripts/hygiene/worktree_inventory.py -- Phase A REPORT-ONLY inventory.

Hermetic: every test builds its own temp git repo(s) under `tmp_path`. Never
inspects the real estate (no worktree count assumptions about this host) —
the real-estate smoke run is a manual step, not part of this suite.

Covers: worktree discovery + grouping, dirty-tree detection, merged vs.
unmerged branch counting, duplicate-.venv detection, and — the hard
constraint — that the read-only git allowlist mechanically rejects every
mutating verb the reaper must never call.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omniagentos.worktrees.git import assert_readonly_git_args, run_readonly_git
from scripts.hygiene import worktree_inventory as wtinv


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=check
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "initial"], root)


def _commit_file(root: Path, name: str, content: str = "x\n") -> None:
    (root / name).write_text(content, encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", f"add {name}"], root)


def _status_porcelain(root: Path) -> str:
    return _git(["status", "--porcelain"], root).stdout


# --------------------------------------------------------------------------
# THE HARD CONSTRAINT: the read-only allowlist rejects every mutating verb
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["worktree", "remove", "/tmp/somewhere"],
        ["worktree", "remove", "--force", "/tmp/somewhere"],
        ["worktree", "prune"],
        ["worktree", "add", "/tmp/somewhere", "main"],
        ["branch", "-d", "some-branch"],
        ["branch", "-D", "some-branch"],
        ["branch", "some-new-branch"],
        ["gc"],
        ["gc", "--prune=now"],
        ["push"],
        ["push", "origin", "main"],
        ["checkout", "main"],
        ["checkout", "-b", "x"],
        ["reset", "--hard"],
        ["reset", "--hard", "HEAD~1"],
        ["clean", "-fd"],
        ["commit", "-m", "x"],
        ["merge", "some-branch"],
        ["rebase", "main"],
        ["stash"],
        ["tag", "-d", "v1"],
        ["remote", "add", "x", "y"],
        [],
    ],
)
def test_allowlist_rejects_every_mutating_verb(args: list[str]) -> None:
    with pytest.raises(ValueError):
        assert_readonly_git_args(args)


@pytest.mark.parametrize(
    "args",
    [
        ["worktree", "list"],
        ["worktree", "list", "--porcelain"],
        ["status"],
        ["status", "--porcelain"],
        ["rev-list", "--left-right", "--count", "a...b"],
        ["rev-parse", "HEAD"],
        ["for-each-ref", "--format=%(refname:short)", "--merged=main", "refs/heads"],
        ["merge-base", "--is-ancestor", "a", "b"],
        ["ls-files"],
        ["log", "-1"],
        ["show", "HEAD"],
    ],
)
def test_allowlist_accepts_the_readonly_calls_this_module_uses(args: list[str]) -> None:
    assert_readonly_git_args(args)  # must not raise


def test_run_readonly_git_never_invokes_subprocess_for_a_rejected_command(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    with pytest.raises(ValueError):
        run_readonly_git(["worktree", "remove", str(tmp_path)], str(tmp_path))
    # Nothing mutated: repo is exactly as it was.
    assert _status_porcelain(tmp_path) == ""


# --------------------------------------------------------------------------
# Discovery + grouping
# --------------------------------------------------------------------------


def test_list_worktrees_finds_main_plus_linked_worktrees(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt_root = tmp_path / "wts"
    wt_root.mkdir()
    wt1 = wt_root / "lane-a"
    _git(["worktree", "add", "-b", "lane/a", str(wt1), "main"], repo)

    entries = wtinv.list_worktrees(repo)
    paths = {Path(e["worktree"]).resolve() for e in entries}
    assert repo.resolve() in paths
    assert wt1.resolve() in paths


def test_classify_root_labels_known_roots(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    known = ((("custom-root", tmp_path / "custom"),))
    inside_custom = tmp_path / "custom" / "lane-x"
    assert wtinv.classify_root(inside_custom, repo, known) == "custom-root"

    dot_claude = repo / ".claude" / "worktrees" / "lane-y"
    assert wtinv.classify_root(dot_claude, repo, known) == ".claude/worktrees"

    unrelated = tmp_path / "totally" / "elsewhere" / "lane-z"
    assert wtinv.classify_root(unrelated, repo, known) == "other"


# --------------------------------------------------------------------------
# Dirty-tree detection
# --------------------------------------------------------------------------


def test_worktree_dirty_count_zero_for_clean_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert wtinv.worktree_dirty_count(repo) == 0


def test_worktree_dirty_count_detects_uncommitted_and_untracked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    (repo / "new_untracked.txt").write_text("new\n", encoding="utf-8")
    count = wtinv.worktree_dirty_count(repo)
    assert count == 2


def test_build_report_counts_dirty_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt_root = tmp_path / "wts"
    wt_root.mkdir()
    wt1 = wt_root / "lane-dirty"
    _git(["worktree", "add", "-b", "lane/dirty", str(wt1), "main"], repo)
    (wt1 / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

    report = wtinv.build_report(repo)
    assert report["counts"]["worktrees_dirty"] >= 1
    dirty_paths = {
        w["path"] for w in report["worktrees"] if (w["dirty_count"] or 0) > 0
    }
    assert str(wt1.resolve()) in {str(Path(p).resolve()) for p in dirty_paths}


# --------------------------------------------------------------------------
# Merged vs. unmerged branch counting
# --------------------------------------------------------------------------


def test_branch_merge_counts_separates_merged_and_unmerged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    # A branch that is fully merged into main (no divergent commits).
    _git(["branch", "lane/already-merged"], repo)

    # A branch with a commit main does NOT have — unmerged.
    _git(["checkout", "-q", "-b", "lane/unmerged"], repo)
    _commit_file(repo, "unmerged_only.txt")
    _git(["checkout", "-q", "main"], repo)

    counts = wtinv.branch_merge_counts(repo, base="main")
    assert counts["total"] == 3  # main, lane/already-merged, lane/unmerged
    assert counts["merged"] == 2  # main itself + lane/already-merged
    assert counts["unmerged"] == 1  # lane/unmerged


def test_build_report_includes_branch_counts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(["checkout", "-q", "-b", "lane/unmerged"], repo)
    _commit_file(repo, "unmerged_only.txt")
    _git(["checkout", "-q", "main"], repo)

    report = wtinv.build_report(repo)
    assert report["counts"]["branches_unmerged_to_main"] == 1
    assert report["counts"]["branches_local_total"] == 2


# --------------------------------------------------------------------------
# Duplicate-.venv detection
# --------------------------------------------------------------------------


def test_worktree_venv_info_detects_presence_and_size(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    venv = repo / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "pkg.py").write_text("x = 1\n" * 1000, encoding="utf-8")

    has_venv, size = wtinv.worktree_venv_info(repo)
    assert has_venv is True
    assert size is not None
    assert size > 0


def test_worktree_venv_info_absent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    has_venv, size = wtinv.worktree_venv_info(repo)
    assert has_venv is False
    assert size is None


def test_build_report_counts_duplicate_venvs_across_worktrees(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt_root = tmp_path / "wts"
    wt_root.mkdir()
    wt1 = wt_root / "lane-venv-a"
    wt2 = wt_root / "lane-venv-b"
    _git(["worktree", "add", "-b", "lane/venv-a", str(wt1), "main"], repo)
    _git(["worktree", "add", "-b", "lane/venv-b", str(wt2), "main"], repo)
    for wt in (wt1, wt2):
        venv_bin = wt / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_text("#!/bin/sh\n", encoding="utf-8")

    report = wtinv.build_report(repo)
    assert report["counts"]["worktrees_with_venv"] == 2


# --------------------------------------------------------------------------
# Zero-mutation proof
# --------------------------------------------------------------------------


def test_build_report_mutates_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt_root = tmp_path / "wts"
    wt_root.mkdir()
    wt1 = wt_root / "lane-a"
    _git(["worktree", "add", "-b", "lane/a", str(wt1), "main"], repo)
    (wt1 / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    before_main = _status_porcelain(repo)
    before_wt = _status_porcelain(wt1)
    before_branches = _git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads"], repo
    ).stdout

    wtinv.build_report(repo)

    assert _status_porcelain(repo) == before_main
    assert _status_porcelain(wt1) == before_wt
    after_branches = _git(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads"], repo
    ).stdout
    assert after_branches == before_branches
    # The worktree registration itself is untouched too.
    after_list = _git(["worktree", "list", "--porcelain"], repo).stdout
    before_list_paths = {"lane-a"}
    assert any(name in after_list for name in before_list_paths)


# --------------------------------------------------------------------------
# Stale worktree registration (path removed without `worktree remove`)
# --------------------------------------------------------------------------


def test_stale_worktree_registration_reported_as_not_exists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    wt_root = tmp_path / "wts"
    wt_root.mkdir()
    wt1 = wt_root / "lane-stale"
    _git(["worktree", "add", "-b", "lane/stale", str(wt1), "main"], repo)

    # Manually delete the directory WITHOUT `git worktree remove` (that's the
    # only way a mutation-free tool could ever see a stale registration).
    import shutil

    shutil.rmtree(wt1)

    report = wtinv.build_report(repo)
    stale = [
        w for w in report["worktrees"] if Path(w["path"]).resolve() == wt1.resolve()
    ]
    assert stale, "stale entry should still be listed by git worktree list --porcelain"
    assert stale[0]["exists"] is False
    assert report["counts"]["worktrees_stale_registration"] == 1


# --------------------------------------------------------------------------
# Human summary formatting doesn't blow up
# --------------------------------------------------------------------------


def test_format_summary_is_nonempty_text(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    report = wtinv.build_report(repo)
    summary = wtinv.format_summary(report)
    assert "worktree" in summary.lower()
    assert str(report["counts"]["worktrees_total"]) in summary
