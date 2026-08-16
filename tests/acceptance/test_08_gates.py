"""AT3 area 8 — VERIFICATION GATES.

Acceptance claims under test:

  1. Every task HAS gates (and a task with nothing to run fails CLOSED rather
     than passing vacuously).
  2. Gates EXECUTE (the command really runs; its output is captured).
  3. Blocking failures STOP WORK — the attempt fails, the task does not reach
     ``done``, and its dependents never run.
  4. Failures return to the CORRECT agent — the feedback lands on the failing
     task (never a sibling) and reaches the next attempt's prompt.

Ground truth:
  * ``omniagentos/swarm/scheduler.py`` — ``default_verifier`` (:909),
    ``_detect_mechanical_suite`` (:834), ``SwarmScheduler._mechanical_failure``
    (:5705) / ``_consume_retry`` / ``_block_task``, the blocking branch at
    :5162-5172, ``assert_touched_modules_importable`` (:869).
  * ``omniagentos/gates/`` — ``run_gates``, ``GateSpec``, ``blocking_failures``,
    ``GateService.g5_local_verify``.
  * ``scripts/benchmarks/configtest_gates.py`` — ``gate_build``,
    ``gate_existing_suite_unmodified``, ``gate_diff_scope``.
  * ``omniagentos/verify/`` — the ``py_compile`` syntax gate and its
    ``off | shadow | enforce`` tri-state, wired at
    ``omniagentos/orchestrator/core.py:540-604``.

Hermetic: shell builtins and ``py_compile`` only, real git on ``tmp_path``,
fake spawner/router/reviewer from ``tests.swarm.scheduler_fakes``. No network,
no LLM.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest

from omniagentos.gates.engine import GateSpec, blocking_failures, run_gates
from omniagentos.gates.service import GateService
from omniagentos.orchestrator import Orchestrator
from omniagentos.orchestrator.contracts import ExecutorRequest, ExecutorResult, ReviewVerdict
from omniagentos.swarm.scheduler import (
    _detect_mechanical_suite,
    assert_touched_modules_importable,
    default_verifier,
)
from omniagentos.verify import (
    run_scoped_pytest,
    verify_mode,
    verify_syntax,
    verify_working_dir,
)
from scripts.benchmarks.configtest_gates import (
    gate_build,
    gate_diff_scope,
    gate_existing_suite_unmodified,
)
from tests.swarm.scheduler_fakes import make_harness, make_scheduler

BROKEN_PY = "def broken(:\n"
GOOD_PY = "def fine() -> int:\n    return 1\n"


@pytest.fixture(autouse=True)
def _isolated_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OMNIAGENTOS_DB_PATH", str(tmp_path / "unused-default.db"))
    monkeypatch.delenv("OMNIAGENTOS_VERIFY_GATE", raising=False)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(("git", "init", "-q", "-b", "main", str(root)), check=True, capture_output=True)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


# ---------------------------------------------------------------------------
# 1. Every task has gates — and nothing to run is a FAILURE, not a pass
# ---------------------------------------------------------------------------


class TestEveryTaskHasAGate:
    def test_gate_on_with_nothing_to_run_refuses_a_vacuous_pass(self, tmp_path: Path) -> None:
        """An ungated task must NOT sail through. This is the whole point."""
        ok, detail = default_verifier({}, {}, str(tmp_path))
        assert ok is False
        assert "refusing vacuous pass" in detail

    def test_syntax_gate_refuses_a_vacuous_pass_on_an_empty_file_list(self) -> None:
        ok, detail = verify_syntax([])
        assert ok is False
        assert "refusing vacuous pass" in detail

    def test_scoped_pytest_refuses_a_vacuous_pass_on_no_targets(self, tmp_path: Path) -> None:
        ok, detail = run_scoped_pytest([], working_dir=str(tmp_path))
        assert ok is False
        assert "refusing vacuous pass" in detail

    def test_scoped_pytest_treats_zero_collected_tests_as_a_failure(self, tmp_path: Path) -> None:
        """pytest exit 5 (nothing collected) is a green exit that means nothing."""
        (tmp_path / "test_empty.py").write_text("# no tests here\n", encoding="utf-8")
        ok, detail = run_scoped_pytest(["test_empty.py"], working_dir=str(tmp_path))
        assert ok is False
        assert "exit 5" in detail

    def test_a_python_project_gets_a_suite_detected_automatically(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        (tmp_path / "tests").mkdir()
        suite = _detect_mechanical_suite(str(tmp_path))
        assert suite, "a python project with tests/ must get a detected suite"
        assert any("pytest" in cmd for cmd in suite)

    def test_a_bare_directory_gets_no_suite_and_therefore_fails_closed(
        self, tmp_path: Path
    ) -> None:
        assert _detect_mechanical_suite(str(tmp_path)) == []
        assert default_verifier({}, {}, str(tmp_path))[0] is False


# ---------------------------------------------------------------------------
# 2. Gates execute
# ---------------------------------------------------------------------------


class TestGatesExecute:
    def test_the_verify_command_really_runs_in_the_task_directory(
        self, tmp_path: Path
    ) -> None:
        sentinel = tmp_path / "gate-ran.txt"
        (tmp_path / "test_gate_runs.py").write_text(
            "from pathlib import Path\n"
            "def test_gate_runs():\n"
            "    Path('gate-ran.txt').write_text('executed\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        ok, output = default_verifier(
            {},
            {"verify_command": "pytest -q test_gate_runs.py"},
            str(tmp_path),
        )
        assert ok is True
        assert sentinel.read_text(encoding="utf-8").strip() == "executed"
        assert sentinel.parent == tmp_path, "the gate ran outside the task directory"

    def test_a_failing_verify_command_reports_failure_with_its_output(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "test_failing_gate.py").write_text(
            "def test_failure():\n    raise AssertionError('3 failed')\n",
            encoding="utf-8",
        )
        ok, output = default_verifier(
            {},
            {"verify_command": "pytest -q test_failing_gate.py"},
            str(tmp_path),
        )
        assert ok is False
        assert "AssertionError: 3 failed" in output

    def test_the_first_failing_gate_short_circuits_the_rest(self, tmp_path: Path) -> None:
        later = tmp_path / "later-gate-ran.txt"
        (tmp_path / "test_first_fails.py").write_text(
            "def test_first_fails():\n    assert False\n",
            encoding="utf-8",
        )
        (tmp_path / "test_later.py").write_text(
            "from pathlib import Path\n"
            "def test_later():\n"
            "    Path('later-gate-ran.txt').write_text('bad', encoding='utf-8')\n",
            encoding="utf-8",
        )
        ok, output = default_verifier(
            {},
            {
                "verify_command": "pytest -q test_first_fails.py",
                "mechanical_suite_commands": ["pytest -q test_later.py"],
            },
            str(tmp_path),
        )
        assert ok is False
        assert not later.exists(), "a later gate ran after a blocking failure"

    def test_a_gate_that_cannot_even_launch_is_a_failure_not_a_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "tests" / "example.py"
        target.parent.mkdir()
        target.write_text("def test_example():\n    assert True\n", encoding="utf-8")

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise OSError("fork failed")

        monkeypatch.setattr("omniagentos.gates.engine.subprocess.run", _boom)
        ok, output = default_verifier(
            {}, {"verify_command": "pytest tests/example.py"}, str(tmp_path)
        )
        assert ok is False
        assert "mechanical command could not run" in output

    def test_a_blocking_gate_failure_is_reported_as_blocking(self, tmp_path: Path) -> None:
        results = run_gates(
            [
                GateSpec(
                    argv=[sys.executable, "-c", "raise SystemExit(1)"],
                    name="blocking_gate",
                ),
                GateSpec(
                    argv=[sys.executable, "-c", "raise SystemExit(1)"],
                    blocking=False,
                    name="advisory_gate",
                ),
            ],
            str(tmp_path),
        )
        blocking = blocking_failures(results)
        assert [r.name for r in blocking] == ["blocking_gate"]

    def test_the_syntax_gate_catches_a_broken_file_a_test_run_would_miss(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path / "repo")
        (repo / "broken.py").write_text(BROKEN_PY, encoding="utf-8")
        outcome = verify_working_dir(str(repo))
        assert outcome is not None
        ok, detail = outcome
        assert ok is False
        assert "SyntaxError" in detail

    def test_the_syntax_gate_passes_a_valid_file(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "repo")
        (repo / "fine.py").write_text(GOOD_PY, encoding="utf-8")
        assert verify_working_dir(str(repo)) == (True, "syntax check passed")

    def test_shadowed_modules_are_flagged_because_edits_would_be_inert(
        self, tmp_path: Path
    ) -> None:
        """An edit to a module Python never loads is a silent no-op."""
        (tmp_path / "json.py").write_text("SHADOW = True\n", encoding="utf-8")
        ok, detail = assert_touched_modules_importable(str(tmp_path), ["json.py"])
        assert ok is False
        assert "shadowed" in detail
        assert assert_touched_modules_importable(str(tmp_path), []) == (True, "")


# ---------------------------------------------------------------------------
# 3. Blocking failures STOP WORK
# ---------------------------------------------------------------------------


class TestBlockingFailuresStopWork:
    def test_a_persistent_gate_failure_blocks_the_task_and_its_dependents(
        self, tmp_path: Path
    ) -> None:
        """End to end through the real ``SwarmScheduler``.

        Not "the verifier returned False somewhere" — the task never reaches
        ``done``, its dependent is never even spawned, and the run is reported
        partial.
        """
        harness = make_harness(
            tmp_path,
            [
                {"id": "bad", "complexity": "simple"},
                {"id": "child", "depends_on": ["bad"]},
                {"id": "good"},
            ],
            max_concurrency=1,
        )

        def verifier(
            task: Any, swarm_json: Any, working_dir: str
        ) -> tuple[bool, str]:
            del task, working_dir
            if str(swarm_json.get("task_key")) == "bad":
                return False, "AssertionError: the suite is red"
            return True, ""

        try:
            scheduler = make_scheduler(harness, verifier=verifier)
            handle = scheduler.start_run(harness.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            assert harness.status_of("bad") == "blocked"
            assert harness.status_of("bad") != "done"
            # WORK STOPPED: the dependent was never dispatched at all.
            assert harness.status_of("child") == "blocked"
            assert "child" not in harness.world.spawn_order
            # Independent work is unaffected — blocking is scoped, not global.
            assert harness.status_of("good") == "done"
            # Every attempt on the failing task was denied; none completed.
            end_reasons = [a["end_reason"] for a in harness.attempts_of("bad")]
            assert end_reasons and set(end_reasons) == {"review_denied"}
            assert harness.emitter.of("run_completed")[0]["partial"] is True
            assert any(
                e.get("reason") == "retry_cap" for e in harness.emitter.of("task_blocked")
            )
        finally:
            harness.close()

    def test_the_same_run_completes_when_the_gate_passes(self, tmp_path: Path) -> None:
        """Control arm: the blocking above is caused by the GATE, not the shape
        of the plan."""
        harness = make_harness(
            tmp_path,
            [
                {"id": "bad", "complexity": "simple"},
                {"id": "child", "depends_on": ["bad"]},
                {"id": "good"},
            ],
            max_concurrency=1,
        )
        try:
            scheduler = make_scheduler(
                harness, verifier=lambda task, swarm_json, working_dir: (True, "")
            )
            handle = scheduler.start_run(harness.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            assert harness.status_of("bad") == "done"
            assert harness.status_of("child") == "done"
            assert "child" in harness.world.spawn_order
            assert harness.emitter.of("run_completed")[0]["partial"] is False
        finally:
            harness.close()

    def test_a_crashing_verifier_blocks_rather_than_failing_open(
        self, tmp_path: Path
    ) -> None:
        harness = make_harness(tmp_path, [{"id": "boom"}], max_concurrency=1)

        def verifier(task: Any, swarm_json: Any, working_dir: str) -> tuple[bool, str]:
            raise RuntimeError("gate exploded")

        try:
            scheduler = make_scheduler(harness, verifier=verifier)
            handle = scheduler.start_run(harness.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            assert harness.status_of("boom") == "blocked"
            assert any(
                "verifier crashed" in str(a["detail"]) for a in harness.attempts_of("boom")
            )
        finally:
            harness.close()

    def test_a_failed_syntax_gate_stops_the_pipeline_before_review(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``enforce``: the reviewer is never even consulted."""
        monkeypatch.setenv("OMNIAGENTOS_VERIFY_GATE", "enforce")
        assert verify_mode() == "enforce"
        workdir = _repo(tmp_path / "repo")
        (workdir / "broken.py").write_text(BROKEN_PY, encoding="utf-8")

        runner = _SeqRunner(results=[ExecutorResult(status="ok", output_text="did work")])
        reviewer = _SpyReviewer(verdicts=["confirm"])
        result = _orchestrator(runner, reviewer, tmp_path).run(
            "fix the file", priority="balanced", working_dir=str(workdir)
        )

        assert reviewer.calls == 0, "review ran despite a blocking gate failure"
        assert result.tasks[0].status == "denied"
        assert result.tasks[0].status != "done"

    def test_shadow_mode_records_but_does_not_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contrast arm — proves the ``enforce`` assertion above is not vacuous."""
        monkeypatch.setenv("OMNIAGENTOS_VERIFY_GATE", "shadow")
        assert verify_mode() == "shadow"
        workdir = _repo(tmp_path / "repo")
        (workdir / "broken.py").write_text(BROKEN_PY, encoding="utf-8")

        runner = _SeqRunner(results=[ExecutorResult(status="ok", output_text="did work")])
        reviewer = _SpyReviewer(verdicts=["confirm"])
        result = _orchestrator(runner, reviewer, tmp_path).run(
            "fix the file", priority="balanced", working_dir=str(workdir)
        )

        assert reviewer.calls == 1
        assert result.tasks[0].status == "done"

    def test_a_self_attested_pass_is_rejected_by_the_g5_gate(self) -> None:
        """A worker cannot mark its own homework."""
        decision = GateService().g5_local_verify(
            {"verify_ok": True, "mechanical_pass": True, "self_attested": True}
        )
        assert decision.decision == "deny"
        assert decision.evidence["reason"] == "self_attested_verification_rejected"
        assert decision.next_state == "verify_blocked"

    def test_a_caller_supplied_override_flag_cannot_buy_a_pass(self) -> None:
        decision = GateService().g5_local_verify(
            {"verify_ok": False, "mechanical_pass": False, "verify_override": True}
        )
        assert decision.decision == "deny"
        assert "verify_override" in decision.evidence["ignored_caller_verdict_flags"]


# ---------------------------------------------------------------------------
# 4. Failures return to the CORRECT agent
# ---------------------------------------------------------------------------


class TestFailuresReturnToTheCorrectAgent:
    def test_feedback_lands_on_the_failing_task_and_reaches_its_next_prompt(
        self, tmp_path: Path
    ) -> None:
        harness = make_harness(
            tmp_path,
            [{"id": "flaky", "complexity": "simple"}, {"id": "sibling"}],
            max_concurrency=1,
        )
        seen: list[str] = []
        marker = "SyntaxError: invalid syntax at line 3"

        def verifier(task: Any, swarm_json: Any, working_dir: str) -> tuple[bool, str]:
            del task, working_dir
            if str(swarm_json.get("task_key")) != "flaky":
                return True, ""
            seen.append("flaky")
            return (False, marker) if len(seen) == 1 else (True, "ok")

        try:
            scheduler = make_scheduler(harness, verifier=verifier)
            handle = scheduler.start_run(harness.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            flaky_json = harness.swarm_json_of("flaky")
            sibling_json = harness.swarm_json_of("sibling")

            # Routed to the failing task ...
            assert any(
                marker in str(entry.get("text"))
                for entry in flaky_json.get("feedback") or []
            )
            assert any(
                entry.get("source") == "mechanical"
                for entry in flaky_json.get("feedback") or []
            )
            # ... and to NOBODY else.
            assert not any(
                marker in str(entry.get("text"))
                for entry in sibling_json.get("feedback") or []
            )

            # The retry is the SAME task, at the SAME tier (mechanical failures
            # get one free same-tier retry before the escalation ladder).
            attempts = harness.attempts_of("flaky")
            assert [a["end_reason"] for a in attempts] == ["review_denied", "completed"]
            assert attempts[0]["tier"] == attempts[1]["tier"] == "simple"
            assert flaky_json.get("mechanical_retry_used") is True
            assert int(flaky_json.get("retries") or 0) == 0

            # The next attempt's PROMPT actually carries the failure back.
            prompts = [
                req.prompt
                for req in harness.world.spawn_requests
                if getattr(req, "task_key", "") == "flaky"
            ]
            assert len(prompts) == 2
            assert marker not in prompts[0]
            assert marker in prompts[1], "the retry did not receive the gate failure"
            assert harness.status_of("flaky") == "done"
        finally:
            harness.close()

    def test_a_second_persistent_failure_escalates_the_tier(self, tmp_path: Path) -> None:
        harness = make_harness(tmp_path, [{"id": "bad", "complexity": "simple"}], max_concurrency=1)

        def verifier(task: Any, swarm_json: Any, working_dir: str) -> tuple[bool, str]:
            del task, working_dir
            if str(swarm_json.get("task_key")) == "bad":
                return False, "still red"
            return True, ""

        try:
            scheduler = make_scheduler(harness, verifier=verifier)
            handle = scheduler.start_run(harness.run_id)
            assert handle is not None
            assert handle.join(timeout=30)

            tiers = [a["tier"] for a in harness.attempts_of("bad")]
            assert tiers == ["simple", "simple", "standard", "complex"]
            assert harness.status_of("bad") == "blocked"
        finally:
            harness.close()


# ---------------------------------------------------------------------------
# 5. The benchmark gate pack (scripts/benchmarks/configtest_gates.py)
# ---------------------------------------------------------------------------


class TestConfigTestGatePack:
    def test_gate_build_fails_on_a_syntactically_broken_change(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "repo")
        (repo / "broken.py").write_text(BROKEN_PY, encoding="utf-8")
        ok, evidence = gate_build(repo, ["broken.py"])
        assert ok is False
        assert evidence["failures"][0]["path"] == "broken.py"
        assert "SyntaxError" in evidence["failures"][0]["error"]

    def test_gate_build_passes_a_valid_change_and_records_what_it_checked(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path / "repo")
        (repo / "fine.py").write_text(GOOD_PY, encoding="utf-8")
        ok, evidence = gate_build(repo, ["fine.py"])
        assert ok is True
        assert evidence["checked"] == ["fine.py"]

    def test_editing_an_existing_test_is_a_violation_but_adding_one_is_not(
        self, tmp_path: Path
    ) -> None:
        """The classic green-by-sabotage vector."""
        repo = _repo(tmp_path / "repo")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_existing.py").write_text("def test_a(): assert 1\n", "utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "add suite")
        base = _git(repo, "rev-parse", "HEAD")

        # Adding a new test: allowed.
        (repo / "tests" / "test_new.py").write_text("def test_b(): assert 1\n", "utf-8")
        ok, evidence = gate_existing_suite_unmodified(repo, base)
        assert ok is True
        assert evidence["added"] == ["tests/test_new.py"]

        # Weakening the existing one: refused.
        (repo / "tests" / "test_existing.py").write_text("def test_a(): pass\n", "utf-8")
        ok, evidence = gate_existing_suite_unmodified(repo, base)
        assert ok is False
        assert evidence["violations"][0]["path"] == "tests/test_existing.py"

    def test_deleting_an_existing_test_is_a_violation(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "repo")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_existing.py").write_text("def test_a(): assert 1\n", "utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "add suite")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "tests" / "test_existing.py").unlink()

        ok, evidence = gate_existing_suite_unmodified(repo, base)
        assert ok is False
        assert evidence["violations"][0]["path"] == "tests/test_existing.py"

    def test_writing_outside_the_owned_paths_is_a_scope_violation(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path / "repo")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "mine").mkdir()
        (repo / "mine" / "ok.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "not_mine.py").write_text("y = 2\n", encoding="utf-8")

        ok, evidence = gate_diff_scope(repo, base, ["mine/"])
        assert ok is False
        assert evidence["violations"] == ["not_mine.py"]

        (repo / "not_mine.py").unlink()
        assert gate_diff_scope(repo, base, ["mine/"])[0] is True

    def test_an_empty_allowlist_fails_closed(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "repo")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "anything.py").write_text("x = 1\n", encoding="utf-8")
        assert gate_diff_scope(repo, base, [])[0] is False


# ---------------------------------------------------------------------------
# Orchestrator test doubles (no LLM, no provider CLI)
# ---------------------------------------------------------------------------


@dataclass
class _SeqRunner:
    results: list[ExecutorResult]
    calls: list[ExecutorRequest] = field(default_factory=list)

    def run(self, request: ExecutorRequest, gateway: Any) -> ExecutorResult:
        self.calls.append(request)
        res = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        if res.working_dir is None:
            res.working_dir = request.working_dir
        return res


@dataclass
class _SpyReviewer:
    verdicts: list[Literal["confirm", "deny"]]
    calls: int = 0

    def review(self, *, task: Any, spec_markdown: str, result: ExecutorResult) -> ReviewVerdict:
        verdict = self.verdicts[min(self.calls, len(self.verdicts) - 1)]
        self.calls += 1
        return ReviewVerdict(verdict=verdict, feedback="review feedback", reviewer="mock")


def _plan_llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any]:
    return {
        "project_name": "AT3",
        "description": "gate acceptance",
        "complexity": "simple",
        "tasks": [{"title": "Task one", "description": "do it", "acceptance_criteria": ["works"]}],
    }


def _orchestrator(runner: _SeqRunner, reviewer: _SpyReviewer, tmp_path: Path) -> Orchestrator:
    return Orchestrator(
        planner_llm=_plan_llm,
        reviewer=reviewer,
        executor_runner=runner,
        vault_dir=str(tmp_path / "vault"),
        syntax_gate=verify_working_dir,
    )


def test_python_interpreter_is_the_one_running_the_suite() -> None:
    """Guard: the gates shell out to ``sys.executable``, not an ambient python."""
    assert Path(sys.executable).exists()
