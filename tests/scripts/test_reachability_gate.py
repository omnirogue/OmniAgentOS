"""Behavioural checks for the standalone reachability gate."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def reachability_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Reachability Gate Test")
    _git(repo, "config", "user.email", "reachability-gate@example.com")
    gate = repo / "scripts" / "reachability-gate.py"
    gate.parent.mkdir()
    shutil.copy2(REPO_ROOT / "scripts" / "reachability-gate.py", gate)
    gate.chmod(0o755)
    (repo / "omniagentos").mkdir()
    (repo / "omniagentos" / "__init__.py").write_text("", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "candidate")
    return repo


def _commit_candidate(repo: Path, files: dict[str, str]) -> None:
    for relative, source in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "candidate")


def _run_gate(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/reachability-gate.py", "candidate", "main"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_same_module_call_is_a_production_caller(reachability_repo: Path) -> None:
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/reachability_dal.py": (
                "def pump_lane_task_id() -> str:\n"
                "    return 'task'\n\n"
                "def _wired() -> str:\n"
                "    return pump_lane_task_id()\n"
            )
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, result.stdout + result.stderr


def test_init_reexport_without_call_is_not_a_production_caller(
    reachability_repo: Path,
) -> None:
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/reachability_module.py": "def scan_content() -> str:\n    return 'ok'\n",
            "omniagentos/reachability_module/__init__.py": (
                "from ..reachability_module import scan_content\n\n"
                "__all__ = ['scan_content']\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1
    assert "scan_content()" in result.stdout


def test_init_call_is_a_production_caller(reachability_repo: Path) -> None:
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/reachability_module.py": "def scan_content() -> str:\n    return 'ok'\n",
            "omniagentos/reachability_module/__init__.py": (
                "from ..reachability_module import scan_content\n\n"
                "SCAN_RESULT = scan_content()\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, result.stdout + result.stderr


def test_orphaned_symbol_still_refuses(reachability_repo: Path) -> None:
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/reachability_orphan.py": (
                "def orphaned_symbol() -> str:\n    return 'nope'\n"
            )
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 1
    assert "orphaned_symbol()" in result.stdout
