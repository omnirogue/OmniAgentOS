"""Agentless as the CHEAP-lane executor (OMNIAGENTOS_AGENTLESS=1).

Flag OFF: ``_runner_for(CHEAP)`` returns the plain cheap session runner. Flag ON: it
wraps the cheap runner in :class:`AgentlessOrCheapRunner`, which takes over only when
the working dir is a git repo AND a verifier command is configured -- otherwise it
delegates. A selected candidate is applied to the REAL working tree (verified against a
throwaway checkout first); no selection / a failed apply returns an objective error
that, under the cascade, escalates the task to FUSION.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omniagentos.agentless.contracts import (
    AgentlessResult,
    CandidatePatch,
    LocalizationResult,
    VerifiedCandidate,
)
from omniagentos.orchestrator import Orchestrator
from omniagentos.orchestrator import agentless_runner as ar_mod
from omniagentos.orchestrator.agentless_runner import (
    AgentlessOrCheapRunner,
    _apply_to_working_tree,
)
from omniagentos.orchestrator.contracts import ExecutorRequest, ExecutorResult, ExecutorTier
from omniagentos.orchestrator.executor import CheapAdapterRunner


def _request(
    working_dir: str, prompt: str = "fix the bug", model: str | None = "sonnet"
) -> ExecutorRequest:
    return ExecutorRequest(
        task=None,  # type: ignore[arg-type] -- the runner never reads request.task
        prompt=prompt,
        working_dir=working_dir,
        tier=ExecutorTier.CHEAP,
        model=model,
        lane="superfast",
        title="fix",
    )


@dataclass
class _StubWrapped:
    calls: list[ExecutorRequest] = field(default_factory=list)

    def run(self, request: ExecutorRequest, gateway: Any) -> ExecutorResult:
        self.calls.append(request)
        return ExecutorResult(status="ok", output_text="cheap session ran", session_id="ses")


@dataclass
class _StubPipeline:
    result: AgentlessResult
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        task: str,
        repo_dir: str,
        test_cmd: str,
        *,
        n: int = 4,
        adapter: str = "cli-claude",
        model: str | None = None,
    ) -> AgentlessResult:
        self.calls.append(
            {
                "task": task,
                "repo_dir": repo_dir,
                "test_cmd": test_cmd,
                "n": n,
                "adapter": adapter,
                "model": model,
            }
        )
        return self.result


def _loc(repo_dir: str) -> LocalizationResult:
    return LocalizationResult(repo_dir=repo_dir, focus_files=[], top_symbols=[], repo_map="")


def _candidate(
    index: int, diff: str | None, *, passed: bool | None, tail: str
) -> VerifiedCandidate:
    return VerifiedCandidate(
        patch=CandidatePatch(
            index=index,
            diff=diff,
            adapter="cli-claude",
            model="sonnet",
            raw_output="",
            gen_seconds=0.1,
            usage={},
        ),
        applied=diff is not None,
        tests_passed=passed,
        test_output_tail=tail,
        returncode=0 if passed else 1,
        verify_seconds=0.1,
    )


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return out.stdout


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "greet.py").write_text("def greet():\n    return 'hi'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


# --- _runner_for wiring ----------------------------------------------------


def test_flag_off_runner_for_returns_plain_cheap_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIAGENTOS_AGENTLESS", raising=False)
    orch = Orchestrator(vault_dir=str(tmp_path))
    runner = orch._runner_for(ExecutorTier.CHEAP)
    assert isinstance(runner, CheapAdapterRunner)
    assert not isinstance(runner, AgentlessOrCheapRunner)


def test_flag_on_runner_for_wraps_cheap_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS", "1")
    orch = Orchestrator(vault_dir=str(tmp_path))
    runner = orch._runner_for(ExecutorTier.CHEAP)
    assert isinstance(runner, AgentlessOrCheapRunner)
    # Fusion tiers are untouched.
    assert not isinstance(orch._runner_for(ExecutorTier.FUSION), AgentlessOrCheapRunner)


# --- delegation ------------------------------------------------------------


def test_delegates_when_no_test_cmd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS", "1")
    monkeypatch.delenv("OMNIAGENTOS_AGENTLESS_TEST_CMD", raising=False)
    repo = _make_repo(tmp_path)
    wrapped = _StubWrapped()
    pipeline = _StubPipeline(AgentlessResult(task="x", localization=_loc(str(repo))))
    runner = AgentlessOrCheapRunner(wrapped=wrapped, pipeline_fn=pipeline)
    result = runner.run(_request(str(repo)), gateway=None)
    assert result.output_text == "cheap session ran"
    assert len(wrapped.calls) == 1
    assert pipeline.calls == []  # never entered the agentless loop


def test_delegates_when_not_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS", "1")
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS_TEST_CMD", "pytest -q")
    plain = tmp_path / "plain"
    plain.mkdir()
    wrapped = _StubWrapped()
    pipeline = _StubPipeline(AgentlessResult(task="x", localization=_loc(str(plain))))
    runner = AgentlessOrCheapRunner(wrapped=wrapped, pipeline_fn=pipeline)
    result = runner.run(_request(str(plain)), gateway=None)
    assert result.status == "ok" and result.output_text == "cheap session ran"
    assert pipeline.calls == []


# --- agentless takeover ----------------------------------------------------


def test_selected_candidate_applies_to_real_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS", "1")
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS_TEST_CMD", "pytest -q")
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS_N", "3")
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS_ADAPTER", "cli-codex")
    repo = _make_repo(tmp_path)

    # Produce a REAL applicable diff via git, then reset the file so the runner applies it.
    (repo / "greet.py").write_text("def greet():\n    return 'hello'\n")
    diff = _git(repo, "diff")
    _git(repo, "checkout", "--", "greet.py")
    assert (repo / "greet.py").read_text() == "def greet():\n    return 'hi'\n"

    selected = _candidate(1, diff, passed=True, tail="1 passed")
    result_obj = AgentlessResult(
        task="fix",
        localization=_loc(str(repo)),
        candidates=[selected],
        selected=selected,
        selection_reason="only one distinct passing candidate",
    )
    pipeline = _StubPipeline(result_obj)
    runner = AgentlessOrCheapRunner(wrapped=_StubWrapped(), pipeline_fn=pipeline)

    result = runner.run(
        _request(str(repo), prompt="make greet say hello", model="sonnet"), gateway=None
    )

    # Pipeline was called with the wired-through args.
    assert pipeline.calls[0]["task"] == "make greet say hello"
    assert pipeline.calls[0]["repo_dir"] == str(repo)
    assert pipeline.calls[0]["test_cmd"] == "pytest -q"
    assert pipeline.calls[0]["n"] == 3
    assert pipeline.calls[0]["adapter"] == "cli-codex"
    assert pipeline.calls[0]["model"] == "sonnet"
    # Success, and the diff was actually applied to the real working tree.
    assert result.status == "ok"
    assert "only one distinct passing candidate" in result.output_text
    assert "1 passed" in result.output_text
    assert (repo / "greet.py").read_text() == "def greet():\n    return 'hello'\n"


def test_no_selection_returns_error_with_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS", "1")
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS_TEST_CMD", "pytest -q")
    repo = _make_repo(tmp_path)
    failing = _candidate(0, "some diff", passed=False, tail="E   assert 1 == 2")
    result_obj = AgentlessResult(
        task="fix",
        localization=_loc(str(repo)),
        candidates=[failing],
        selected=None,
        selection_reason="no candidate passed tests (#0: tests failed)",
    )
    runner = AgentlessOrCheapRunner(wrapped=_StubWrapped(), pipeline_fn=_StubPipeline(result_obj))
    result = runner.run(_request(str(repo)), gateway=None)
    assert result.status == "error"
    assert "no verified fix" in (result.error or "")
    assert "no candidate passed tests" in (result.error or "")
    assert "assert 1 == 2" in (result.error or "")  # per-candidate test evidence carried
    # The real tree was left untouched.
    assert (repo / "greet.py").read_text() == "def greet():\n    return 'hi'\n"


# --- FIX 1: dirty working tree delegates (verify baseline HEAD != dirty apply target) --


def test_dirty_working_tree_delegates_to_cheap_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS", "1")
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS_TEST_CMD", "pytest -q")
    repo = _make_repo(tmp_path)
    # Uncommitted edit: HEAD (what the verifier checks out) now diverges from the tree.
    (repo / "greet.py").write_text("def greet():\n    return 'dirty'\n")

    wrapped = _StubWrapped()
    pipeline = _StubPipeline(AgentlessResult(task="x", localization=_loc(str(repo))))
    runner = AgentlessOrCheapRunner(wrapped=wrapped, pipeline_fn=pipeline)

    result = runner.run(_request(str(repo)), gateway=None)

    assert result.output_text == "cheap session ran"
    assert len(wrapped.calls) == 1  # delegated
    assert pipeline.calls == []  # agentless loop never entered on a dirty tree


def test_clean_working_tree_takes_pipeline_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS", "1")
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS_TEST_CMD", "pytest -q")
    repo = _make_repo(tmp_path)  # clean at HEAD

    wrapped = _StubWrapped()
    failing = _candidate(0, "some diff", passed=False, tail="E   boom")
    pipeline = _StubPipeline(
        AgentlessResult(
            task="x",
            localization=_loc(str(repo)),
            candidates=[failing],
            selected=None,
            selection_reason="no candidate passed tests",
        )
    )
    runner = AgentlessOrCheapRunner(wrapped=wrapped, pipeline_fn=pipeline)

    result = runner.run(_request(str(repo)), gateway=None)

    assert wrapped.calls == []  # never delegated
    assert len(pipeline.calls) == 1  # pipeline path taken on a clean tree
    assert result.status == "error"


# --- FIX 3: real-tree apply mirrors the scratch-verify ladder (git apply, then --3way) --


@dataclass
class _FakeProc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def test_apply_to_working_tree_falls_back_to_3way(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> _FakeProc:
        calls.append(cmd)
        # Plain `git apply` (no --3way) fails; the --3way rung succeeds.
        if "--3way" in cmd:
            return _FakeProc(returncode=0)
        return _FakeProc(returncode=1, stderr="patch does not apply")

    monkeypatch.setattr(ar_mod.subprocess, "run", _fake_run)

    applied, detail = _apply_to_working_tree("/some/dir", "a diff")

    assert applied is True
    assert detail == ""
    # Both rungs were attempted, in order: plain apply, then --3way.
    assert len(calls) == 2
    assert "--3way" not in calls[0]
    assert "--3way" in calls[1]


def test_apply_to_working_tree_reports_3way_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(cmd: list[str], **kwargs: Any) -> _FakeProc:
        detail = "3way could not build fake ancestor" if "--3way" in cmd else "plain failed"
        return _FakeProc(returncode=1, stderr=detail)

    monkeypatch.setattr(ar_mod.subprocess, "run", _fake_run)

    applied, detail = _apply_to_working_tree("/some/dir", "a diff")

    assert applied is False
    assert "3way could not build fake ancestor" in detail  # the last rung's detail


# --- FIX 7a: pipeline RAISES -> objective error naming the pipeline fault ---------------


def test_pipeline_raises_returns_objective_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS", "1")
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS_TEST_CMD", "pytest -q")
    repo = _make_repo(tmp_path)

    def _boom(**_kwargs: Any) -> AgentlessResult:
        raise RuntimeError("worktree add exploded")

    runner = AgentlessOrCheapRunner(wrapped=_StubWrapped(), pipeline_fn=_boom)
    result = runner.run(_request(str(repo)), gateway=None)

    assert result.status == "error"
    assert "agentless pipeline raised" in (result.error or "")
    assert "worktree add exploded" in (result.error or "")


# --- FIX 7b: selected candidate whose verified diff will NOT apply to the real tree -----


def test_selected_but_real_tree_apply_fails_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS", "1")
    monkeypatch.setenv("OMNIAGENTOS_AGENTLESS_TEST_CMD", "pytest -q")
    repo = _make_repo(tmp_path)

    # A diff against a file that does not exist in the real tree: fails plain apply AND
    # --3way (no fake ancestor), so the real-tree apply objectively fails.
    bogus_diff = (
        "diff --git a/ghost.py b/ghost.py\n"
        "index 0000000..1111111 100644\n"
        "--- a/ghost.py\n"
        "+++ b/ghost.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old line that is not present\n"
        "+new line\n"
    )
    selected = _candidate(2, bogus_diff, passed=True, tail="1 passed")
    result_obj = AgentlessResult(
        task="fix",
        localization=_loc(str(repo)),
        candidates=[selected],
        selected=selected,
        selection_reason="only one distinct passing candidate",
    )
    runner = AgentlessOrCheapRunner(wrapped=_StubWrapped(), pipeline_fn=_StubPipeline(result_obj))

    result = runner.run(_request(str(repo)), gateway=None)

    assert result.status == "error"
    assert "failed to apply to the working tree" in (result.error or "")
    assert "#2" in (result.error or "")  # names the selected candidate
    # The real tree was left untouched.
    assert (repo / "greet.py").read_text() == "def greet():\n    return 'hi'\n"
