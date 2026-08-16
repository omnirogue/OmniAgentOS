"""Test for false-green defect in reachability gate (word-match only).

This test reproduces the bug: a module with exported functions named 'fetch' and 'search'
matched 31 unrelated files (like 'util.py' containing "search for files" in a comment)
and falsely passed the gate with ZERO real callers.
"""

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


def test_false_green_word_match_only(reachability_repo: Path) -> None:
    """Demonstrate the false-green bug: bare word match in unrelated files passes.

    OLD BEHAVIOR (BUGGY):
    - Module has fetch() and search() (common words)
    - Unrelated files contain "fetch" in comments/strings/docstrings
    - git grep finds all those files
    - _production_callers() adds them to hits WITHOUT checking actual imports/calls
    - Result: gate PASSES (false green) with ZERO real callers

    NEW BEHAVIOR (FIXED):
    - Same setup, but AST analysis checks EVERY file (not just defining module)
    - For each file, verify actual `import module` or `from module import symbol`
    - Then verify the imported name is actually called as a function
    - Result: gate REFUSES (correctly red) because there are NO real callers
    """
    _commit_candidate(
        reachability_repo,
        {
            # The module with exported symbols
            "omniagentos/search_module.py": (
                "def fetch() -> str:\n"
                '    """Fetch data from somewhere."""\n'
                "    return 'fetched'\n\n"
                "def search() -> str:\n"
                '    """Search for something."""\n'
                "    return 'found'\n"
            ),
            # Unrelated files that contain the words "fetch" and "search"
            # These should NOT count as production callers
            "omniagentos/util.py": (
                "# This module helps you fetch and search for data\n"
                "# We search the database for items\n"
                "def _process_items():\n"
                "    pass\n"
            ),
            "omniagentos/database.py": (
                "class Database:\n"
                '    """Fetch results from the database, search by ID."""\n'
                "    def _query(self):\n"
                "        pass\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    # The gate MUST refuse because there are NO real callers
    # (word matches in comments/docstrings don't count)
    assert result.returncode == 1, (
        f"Expected gate to REFUSE (returncode=1), but it PASSED (returncode=0).\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "fetch()" in result.stdout, "Should refuse fetch with no caller"
    assert "search()" in result.stdout, "Should refuse search with no caller"


def test_real_caller_with_common_name_still_passes(reachability_repo: Path) -> None:
    """Verify that a TRUE caller of a common-named symbol still passes.

    This is the positive control: when there IS a real import and call,
    even for common names like 'fetch' or 'search', the gate should pass.
    """
    _commit_candidate(
        reachability_repo,
        {
            # Module with common-named exports
            "omniagentos/search_module.py": (
                "def fetch() -> str:\n"
                "    return 'data'\n\n"
                "def search() -> str:\n"
                "    return 'results'\n"
            ),
            # This file ACTUALLY imports and calls the symbols
            "omniagentos/caller.py": (
                "from omniagentos.search_module import fetch, search\n\n"
                "def _get_data():\n"  # Private function (starts with _)
                "    data = fetch()\n"  # Real call to fetch
                "    results = search()\n"  # Real call to search
                "    return data + results\n"
            ),
            # This file DOESN'T import the symbols (word match only)
            "omniagentos/util.py": (
                "# This code will search the results\n"
                "# We fetch items from storage\n"
                "def _process():\n"  # Private function
                "    pass\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    # Should PASS because there is a real caller
    assert result.returncode == 0, (
        f"Expected gate to PASS (returncode=0), but it REFUSED (returncode=1).\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_qualified_call_counts_as_caller(reachability_repo: Path) -> None:
    """Verify that qualified calls (module.symbol) are recognized."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/search_module.py": (
                "def fetch() -> str:\n"
                "    return 'data'\n"
            ),
            # Qualified call: import module, then call module.fetch()
            "omniagentos/caller.py": (
                "import omniagentos.search_module\n\n"
                "def _get_data():\n"
                "    return omniagentos.search_module.fetch()\n"
            ),
        },
    )

    result = _run_gate(reachability_repo)

    assert result.returncode == 0, (
        f"Expected gate to PASS (qualified call), but it REFUSED.\n"
        f"stdout:\n{result.stdout}"
    )


def test_import_without_call_is_not_a_caller(reachability_repo: Path) -> None:
    """Verify that a bare import (without a call) is NOT counted as a caller."""
    _commit_candidate(
        reachability_repo,
        {
            "omniagentos/search_module.py": (
                "def fetch() -> str:\n"
                "    return 'data'\n"
            ),
            # Import but don't call — this should NOT count
            "omniagentos/no_caller.py": (
                "from omniagentos.search_module import fetch\n\n"
                "# We imported fetch but never use it\n"
                "DATA = {'source': 'fetch'}\n"  # fetch is just a string key, not a call
            ),
        },
    )

    result = _run_gate(reachability_repo)

    # Should REFUSE because import without call is not a production caller
    assert result.returncode == 1, (
        f"Expected gate to REFUSE (import without call), but it PASSED.\n"
        f"stdout:\n{result.stdout}"
    )
    assert "fetch()" in result.stdout
