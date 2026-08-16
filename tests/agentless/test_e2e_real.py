"""Flagship end-to-end test: REAL git worktrees, REAL git apply, REAL pytest.

This repo's history has burned us before on fakes hiding a completely broken
loop, so this test drives :func:`run_agentless` against a tiny REAL git repo
with a REAL bug and a REAL failing pytest test. Only the model-generation step
is stubbed (an injected ``resolve`` returning scripted raw outputs) — everything
downstream (localize via real repomap, diff extraction, git worktree/apply,
subprocess pytest verification, selection) runs for real.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from omniagentos.agentless.pipeline import run_agentless
from omniagentos.contracts import (
    AgentAdapter,
    AgentInput,
    AgentResult,
    AgentUsage,
    HealthStatus,
    ResultStatus,
)

_BUGGY_CALC = "def add(a, b):\n    return a - b\n"

_TEST_CALC = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"

# Sample 0: applies cleanly but does NOT fix the bug (adds an inert comment).
_BAD_DIFF = (
    "diff --git a/calc.py b/calc.py\n"
    "index 0000000..1111111 100644\n"
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def add(a, b):\n"
    "+    # attempted fix, does not change behavior\n"
    "     return a - b\n"
)

# Sample 1: the correct fix.
_GOOD_DIFF = (
    "diff --git a/calc.py b/calc.py\n"
    "index 0000000..2222222 100644\n"
    "--- a/calc.py\n"
    "+++ b/calc.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a - b\n"
    "+    return a + b\n"
)

# Sample 2: garbage — the model refuses, no diff fence, no diff-shaped text.
_GARBAGE = "I'm sorry, I can't help with that request."

_SCRIPTED_OUTPUTS = {
    0: f"```diff\n{_BAD_DIFF}```",
    1: f"```diff\n{_GOOD_DIFF}```",
    2: _GARBAGE,
}


class _ScriptedStubAdapter:
    """Deterministic stand-in for a real CLI adapter: NEVER shells out."""

    name = "stub"
    version = "1.0"

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.prompts_seen: list[str] = []
        self.calls: list[int] = []

    def run(self, input: AgentInput) -> AgentResult:
        index = int(input.run_id.rsplit("-", 1)[-1])
        with self.lock:
            self.prompts_seen.append(input.prompt)
            self.calls.append(index)
        raw = _SCRIPTED_OUTPUTS.get(index, "unscripted sample — should never be requested")
        return AgentResult(
            status=ResultStatus.OK,
            output_text=raw,
            usage=AgentUsage(wall_ms=1, turns=1, input_tokens=10, output_tokens=5, cost_usd=0.0),
        )

    def cancel(self, session_ref: str) -> bool:
        return True

    def health(self) -> HealthStatus:
        return HealthStatus(healthy=True, detail="stub")


def _run_git(repo_dir: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo_dir, check=True, capture_output=True, text=True)


def _build_buggy_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "buggy_repo"
    repo_dir.mkdir()
    (repo_dir / "calc.py").write_text(_BUGGY_CALC)
    (repo_dir / "test_calc.py").write_text(_TEST_CALC)
    _run_git(repo_dir, "init", "-q")
    _run_git(repo_dir, "-c", "user.email=test@example.com", "-c", "user.name=Test", "add", "-A")
    _run_git(
        repo_dir,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "-q",
        "-m",
        "initial buggy commit",
    )
    return repo_dir


def test_agentless_e2e_real_git_and_pytest(tmp_path: Path) -> None:
    repo_dir = _build_buggy_repo(tmp_path)
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()

    stub = _ScriptedStubAdapter()

    def resolve(_adapter_name: str) -> AgentAdapter:
        return stub

    # Use the same interpreter as this test run so pytest is importable
    # (bare `python` may resolve to a system/pyenv install without pytest).
    verify_cmd = f"{sys.executable} -m pytest -q test_calc.py"
    result = run_agentless(
        "Fix the bug in calc.py: add(a, b) should return a + b, not a - b.",
        str(repo_dir),
        verify_cmd,
        n=6,
        batch_size=3,
        early_stop=True,
        resolve=resolve,
        scratch_root=str(scratch_root),
        gen_timeout_s=30,
        test_timeout_s=60,
    )

    # --- selection correctness ---
    assert result.selected is not None, f"no candidate selected: {result.selection_reason}"
    assert result.selected.patch.diff is not None
    assert "return a + b" in result.selected.patch.diff
    assert result.selected.tests_passed is True

    by_diff_marker = {c.patch.index: c for c in result.candidates}

    bad_candidate = by_diff_marker.get(0)
    assert bad_candidate is not None
    assert bad_candidate.applied is True  # the inert-comment diff DOES apply cleanly
    assert bad_candidate.tests_passed is False  # but the bug is still there

    garbage_candidate = by_diff_marker.get(2)
    assert garbage_candidate is not None
    assert garbage_candidate.applied is False  # no diff extracted -> never applied
    assert garbage_candidate.tests_passed is None

    # --- early stop: batch containing the passer (0,1,2) ran; batch (3,4,5) did not ---
    assert len(result.candidates) == 3, (
        f"expected early stop after the first batch of 3, got {len(result.candidates)} candidates"
    )
    assert {c.patch.index for c in result.candidates} == {0, 1, 2}

    # --- byte-identical prompt across every sample actually generated ---
    assert len(stub.prompts_seen) == 3
    assert len(set(stub.prompts_seen)) == 1, "every sample must see the SAME rendered prompt"

    # --- scratch dirs cleaned up ---
    assert list(scratch_root.iterdir()) == [], "scratch dirs must be cleaned up after the run"

    # --- original repo untouched ---
    assert (repo_dir / "calc.py").read_text() == _BUGGY_CALC
    worktree_list = subprocess.run(
        ["git", "worktree", "list"], cwd=repo_dir, capture_output=True, text=True, check=True
    ).stdout
    assert len(worktree_list.strip().splitlines()) == 1, "no leftover git worktrees"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True, check=True
    ).stdout
    assert status.strip() == "", f"original repo must be clean, got: {status!r}"
