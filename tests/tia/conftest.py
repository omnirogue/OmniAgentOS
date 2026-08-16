"""Shared builders for the TIA suite: a real (tiny) git repo with a real test tree.

These tests deliberately operate on filesystems and git repositories rather than mocks.
The selector's whole job is to reason about paths that exist, and the two rejected
attempts at this feature both passed their tests while doing nothing, because their tests
never asked the code to look at anything real.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

GIT_ENV = {
    "GIT_AUTHOR_NAME": "tia",
    "GIT_AUTHOR_EMAIL": "tia@example.invalid",
    "GIT_COMMITTER_NAME": "tia",
    "GIT_COMMITTER_EMAIL": "tia@example.invalid",
    # Hermetic: never read the operator's gitconfig (hooks, templates, signing).
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}

#: One test file per always-run pattern family, so a synthetic repo can exercise the
#: critical-set path instead of degrading to FULL for lack of any critical test at all.
CRITICAL_TREE: tuple[str, ...] = (
    "tests/doctrine/test_doctrine.py",
    "tests/counterfeits/test_gate.py",
    "tests/gates/test_engine.py",
    "tests/gates_scripts/test_scripts.py",
    "tests/acceptance/test_01_thing.py",
    "tests/certification/test_dod.py",
    "tests/policy/test_secret_registry.py",
    "tests/api/test_path_containment.py",
    "tests/swarm/test_planner_verifier_security.py",
)


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(repo), **GIT_ENV},
    )
    return proc.stdout


def write(repo: Path, rel: str, text: str = "") -> Path:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


@pytest.fixture
def synthetic_repo(tmp_path: Path) -> Path:
    """A git repo with source files, a full critical-test tree, and two commits."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "pyproject.toml", "[tool.pytest.ini_options]\naddopts = \"-m 'not live'\"\n")
    write(repo, "pkg/mod_a.py", "def add(a, b):\n    return a + b\n")
    write(repo, "pkg/mod_b.py", "def sub(a, b):\n    return a - b\n")
    write(repo, "tests/unit/test_alpha.py", "def test_add():\n    assert True\n")
    write(repo, "tests/unit/test_beta.py", "def test_sub():\n    assert True\n")
    for rel in CRITICAL_TREE:
        write(repo, rel, "def test_placeholder():\n    assert True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    write(repo, "pkg/mod_a.py", "def add(a, b):\n    return b + a\n")
    write(repo, "tests/unit/test_alpha.py", "def test_add():\n    assert True is True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "touch mod_a and its test")
    return repo
