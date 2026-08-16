"""Robustness contracts for :func:`omniagentos.agentless.pipeline.run_agentless`.

Two invariants the reviewer flagged and we now hold with REAL git / REAL threads:

* FIX 2 (never-raise): a hostile repo whose scratch checkout cannot be built (an
  unborn ``HEAD`` -> ``git worktree add`` raises ``CalledProcessError``) is recorded as
  a normal failing candidate; ``run_agentless`` returns an ``AgentlessResult`` instead
  of propagating the exception.
* FIX 5 (hard wall): ``gen_timeout_s`` is a real wall. A hung adapter thread that
  sleeps far past the timeout does NOT block the pipeline on executor shutdown; the run
  returns within the wall with the candidate recorded as timed out.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from omniagentos.agentless.pipeline import run_agentless
from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    AgentUsage,
    HealthStatus,
    ResultStatus,
)

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


class _DiffStubAdapter:
    """Returns a fixed diff fence immediately (never shells out)."""

    name = "stub"
    version = "1.0"

    def run(self, input: AgentInput) -> AgentResult:
        return AgentResult(
            status=ResultStatus.OK,
            output_text=f"```diff\n{_GOOD_DIFF}```",
            usage=AgentUsage(wall_ms=1, turns=1, input_tokens=1, output_tokens=1, cost_usd=0.0),
        )

    def cancel(self, session_ref: str) -> bool:
        return True

    def health(self) -> HealthStatus:
        return HealthStatus(healthy=True, detail="stub")


class _HangingAdapter:
    """``run()`` sleeps far past any sane gen_timeout_s to simulate a stuck CLI."""

    name = "hang"
    version = "1.0"

    def __init__(self, sleep_s: float) -> None:
        self._sleep_s = sleep_s
        self.started = threading.Event()

    def run(self, input: AgentInput) -> AgentResult:
        self.started.set()
        time.sleep(self._sleep_s)
        return AgentResult(status=ResultStatus.OK, output_text="too late")

    def cancel(self, session_ref: str) -> bool:
        return True

    def health(self) -> HealthStatus:
        return HealthStatus(healthy=True, detail="hang")


def _unborn_head_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "unborn"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    # `git init` with NO commit -> HEAD is unborn, so `git worktree add ... HEAD` raises.
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True, text=True)
    return repo


def test_unborn_head_never_raises_records_checkout_error(tmp_path: Path) -> None:
    repo = _unborn_head_repo(tmp_path)
    stub = _DiffStubAdapter()

    # Must NOT raise despite the scratch `git worktree add HEAD` failing.
    result = run_agentless(
        "Fix add() to return a + b",
        str(repo),
        "python -m pytest -q",
        n=1,
        batch_size=1,
        resolve=lambda _name: stub,
        scratch_root=str(tmp_path / "scratch"),
        gen_timeout_s=30,
        test_timeout_s=30,
    )

    assert result.selected is None  # nothing could be verified
    assert len(result.candidates) == 1
    tail = result.candidates[0].test_output_tail
    assert "scratch checkout failed" in tail
    assert result.candidates[0].tests_passed is None
    assert result.candidates[0].applied is False


def test_gen_timeout_is_a_hard_wall(tmp_path: Path) -> None:
    repo = _unborn_head_repo(tmp_path)  # repo contents irrelevant; generation never returns
    hanging = _HangingAdapter(sleep_s=12.0)

    started = time.monotonic()
    result = run_agentless(
        "Fix add()",
        str(repo),
        "python -m pytest -q",
        n=1,
        batch_size=1,
        resolve=lambda _name: hanging,
        scratch_root=str(tmp_path / "scratch"),
        gen_timeout_s=1,  # wall == gen_timeout_s + 5 == ~6s, well under the 12s sleep
        test_timeout_s=30,
    )
    elapsed = time.monotonic() - started

    assert hanging.started.is_set()  # the adapter really did start (and is still sleeping)
    # Generous margin: the pipeline must return on the timeout wall, not the 12s sleep.
    assert elapsed < 11.0, f"pipeline blocked on the hung worker for {elapsed:.1f}s"
    assert len(result.candidates) == 1
    assert "generation exceeded 1s" in result.candidates[0].patch.raw_output
    assert result.selected is None
