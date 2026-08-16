"""Changed-file detection, against real git repositories.

The headline case is :func:`test_untracked_files_are_reported`. A selector that cannot
see a file the developer has not committed yet will decide their new module affects
nothing, run a subset that excludes its brand-new test, and report green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.tia.changes import (
    GitError,
    changed_files,
    changed_files_for_commit,
    recent_commits,
    resolve_base,
)

from .conftest import git, write


def test_untracked_files_are_reported(synthetic_repo: Path) -> None:
    write(synthetic_repo, "pkg/brand_new.py", "X = 1\n")
    write(synthetic_repo, "tests/unit/test_brand_new.py", "def test_x():\n    assert True\n")
    reported = changed_files(synthetic_repo, "HEAD")
    assert "pkg/brand_new.py" in reported
    assert "tests/unit/test_brand_new.py" in reported


def test_without_untracked_detection_a_new_file_is_invisible(synthetic_repo: Path) -> None:
    """Documents exactly what `git diff --name-only` alone loses."""
    write(synthetic_repo, "pkg/brand_new.py", "X = 1\n")
    assert "pkg/brand_new.py" not in changed_files(
        synthetic_repo, "HEAD", include_untracked=False
    )


def test_gitignored_files_are_not_reported(synthetic_repo: Path) -> None:
    write(synthetic_repo, ".gitignore", "ignored/\n")
    git(synthetic_repo, "add", ".gitignore")
    git(synthetic_repo, "commit", "-q", "-m", "ignore")
    write(synthetic_repo, "ignored/thing.py", "X = 1\n")
    assert "ignored/thing.py" not in changed_files(synthetic_repo, "HEAD")


def test_uncommitted_and_staged_edits_are_reported(synthetic_repo: Path) -> None:
    write(synthetic_repo, "pkg/mod_b.py", "def sub(a, b):\n    return a - b - 0\n")
    write(synthetic_repo, "pkg/mod_a.py", "def add(a, b):\n    return a + b + 0\n")
    git(synthetic_repo, "add", "pkg/mod_a.py")
    reported = changed_files(synthetic_repo, "HEAD")
    assert "pkg/mod_a.py" in reported
    assert "pkg/mod_b.py" in reported


def test_committed_changes_since_a_base_are_reported(synthetic_repo: Path) -> None:
    reported = changed_files(synthetic_repo, "HEAD~1")
    assert "pkg/mod_a.py" in reported
    assert "tests/unit/test_alpha.py" in reported


def test_changed_files_for_commit_lists_that_commits_paths(synthetic_repo: Path) -> None:
    head = recent_commits(synthetic_repo, 1)[0]
    assert set(changed_files_for_commit(synthetic_repo, head)) == {
        "pkg/mod_a.py",
        "tests/unit/test_alpha.py",
    }


def test_changed_files_for_the_root_commit_lists_everything(synthetic_repo: Path) -> None:
    root = recent_commits(synthetic_repo, 5)[-1]
    reported = changed_files_for_commit(synthetic_repo, root)
    assert "pkg/mod_b.py" in reported
    assert "tests/doctrine/test_doctrine.py" in reported


def test_recent_commits_is_newest_first(synthetic_repo: Path) -> None:
    commits = recent_commits(synthetic_repo, 5)
    assert len(commits) == 2
    assert commits[0] != commits[1]
    assert commits[0] == git(synthetic_repo, "rev-parse", "HEAD").strip()


def test_recent_commits_of_zero_is_empty(synthetic_repo: Path) -> None:
    assert recent_commits(synthetic_repo, 0) == ()


def test_a_failing_git_command_raises_instead_of_reporting_no_changes(tmp_path: Path) -> None:
    """An empty tuple would read as "nothing changed", i.e. as a safe, wrong answer."""
    with pytest.raises(GitError):
        recent_commits(tmp_path / "not-a-repo", 3)


def test_resolve_base_falls_back_when_main_is_unreachable(synthetic_repo: Path) -> None:
    git(synthetic_repo, "branch", "-m", "main", "elsewhere")
    assert resolve_base(synthetic_repo) == "HEAD~1"


def test_resolve_base_prefers_the_merge_base_with_main(synthetic_repo: Path) -> None:
    base = git(synthetic_repo, "rev-parse", "HEAD").strip()
    git(synthetic_repo, "checkout", "-q", "-b", "feature")
    write(synthetic_repo, "pkg/mod_c.py", "Y = 2\n")
    git(synthetic_repo, "add", "-A")
    git(synthetic_repo, "commit", "-q", "-m", "feature work")
    assert resolve_base(synthetic_repo) == base
    assert "pkg/mod_c.py" in changed_files(synthetic_repo)
