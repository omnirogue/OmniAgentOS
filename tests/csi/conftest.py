"""CSI test fixtures with optimized repository template."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git command in a specific repository."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=check,
    )


def _create_template_repo(tmp_path: Path) -> Path:
    """Create a single template repository with the standard CSI test structure.

    This is called once per xdist worker (via session scope) to build a clean
    template repo. The actual _repo fixture copies this template for each test
    instead of init/config/add/commit, which is ~6 subprocess spawns per test
    avoided.
    """
    repo = tmp_path / "template-repo"
    repo.mkdir(parents=True, exist_ok=True)

    # Initialize the repo
    init = _git(repo, "init", "-b", "main", check=False)
    if init.returncode != 0:
        _git(repo, "init")
        _git(repo, "checkout", "-b", "main")

    # Configure git user
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "CSI Test")

    # Create the standard structure
    (repo / ".gitignore").write_text("var/\n", encoding="utf-8")
    target = repo / "vault" / "skills" / "self-learning" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Original\n", encoding="utf-8")
    (repo / "README.md").write_text("# Temporary CSI repository\n", encoding="utf-8")

    # Stage and commit
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")

    return repo


@pytest.fixture(scope="session")
def _template_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-scoped template repository.

    Each xdist worker gets one template repo. All tests in that worker copy
    from this template instead of reinitializing git from scratch.
    """
    template_root = tmp_path_factory.mktemp("csi-template")
    return _create_template_repo(template_root)


@pytest.fixture
def _repo_factory(_template_repo: Path) -> Callable[[Path], Path]:
    """Factory fixture that creates repos by copying the template.

    This allows tests to call the factory function like the old _repo(tmp_path)
    but with the optimization of copying instead of init/config/commit.

    Usage:
        repo = _repo_factory(tmp_path)  # Creates repo in tmp_path/repo
        state_repo = _repo_factory(tmp_path / "state")  # Creates repo at tmp_path/state/repo
    """
    def make_repo(parent_path: Path) -> Path:
        """Create a repo by copying the template.

        Args:
            parent_path: Parent directory where to create the repo

        Returns:
            Path to the new repo (at parent_path/repo)
        """
        # Ensure parent exists
        parent_path.mkdir(parents=True, exist_ok=True)
        # Copy the template to repo subdirectory
        target_repo = parent_path / "repo"
        shutil.copytree(_template_repo, target_repo, dirs_exist_ok=False)
        return target_repo

    return make_repo
