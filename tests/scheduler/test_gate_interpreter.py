"""The gate must be able to EXECUTE what a routine declares, not merely resolve it.

Two lanes ago the merge gate proved that every loop routine's ``gate_config
.command`` RESOLVED — the targets existed, at a pinned SHA, and collection
worked. It shipped, and the first live settlement of a re-enabled loop was
``gate_passed=0 | accepted=0 | outcome_class=adverse``: ``PytestGateRunner`` ran
the loops suite on the PRODUCTION venv, which has no LangGraph, so
``loops/omniagentos_loops/runtime.py`` raised ``ModuleNotFoundError`` and nine
tests failed. Every loop whose gate names its own tests would have failed that
gate forever and auto-paused after three ticks — false adverse at scale, the
exact failure class the executed-gate work existed to eliminate.

"Resolves" and "passes" are different claims, and only the second one is the one
settlement actually makes. So this module asserts the second for the gate command
that matters: a gate that names a loop instance's OWN tests under ``loops/``,
which is the command the live W3 row carries and the one that produced the
incident. It is executed through the real :class:`PytestGateRunner`, against a
real pinned git workspace, the same way ``routines_settle.settle_routine_run``
does it — no fakes between the assertion and the subprocess.

There used to be a second executed case here: the command
``omniagentos_loops.registry.loop_routine_row`` substituted when an author
declared no gate. That default is GONE (2026-08-02) — it graded the
scheduler→worker mechanism, so it passed on every tick of every loop whatever
the instance produced, and the seeding helper now refuses a row without an
explicit gate. "The default still passes" is not a property this repo has any
more; that the default no longer EXISTS is asserted in
``tests/scheduler/test_loop_gate_refusal.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from omniagentos.scheduler.gate_evidence import (
    GateEvidenceRefusal,
    GateEvidenceStore,
    GateWorkspaceUnusable,
)
from omniagentos.scheduler.gate_runner import (
    INTERPRETER_CLASS_LOOPS,
    INTERPRETER_CLASS_REPO,
    GateRunRequest,
    PytestGateRunner,
    default_loops_interpreter,
    interpreter_class_for_targets,
    produce_gate_evidence,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The gate the live W3 row declares, verbatim. It is the command that produced
#: btrun_15ee5af1dfb249fc9ad6's false adverse.
LOOPS_GATE_COMMAND = "pytest loops/tests/instances/test_health_monitor.py"

PASSING_SUITE = """
def test_one() -> None:
    assert True
"""


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc


def _synthetic_workspace(tmp_path: Path) -> Path:
    """A tiny git checkout shaped like the repo: a ``loops/`` tree and a prod tree."""
    root = tmp_path / "workspace"
    (root / "loops" / "tests").mkdir(parents=True)
    (root / "tests" / "scheduler").mkdir(parents=True)
    (root / "loops" / "tests" / "test_loopy.py").write_text(PASSING_SUITE, encoding="utf-8")
    (root / "tests" / "scheduler" / "test_prod.py").write_text(PASSING_SUITE, encoding="utf-8")
    (root / ".gitignore").write_text(".pytest_cache\n__pycache__\nvar\n", encoding="utf-8")
    _git("init", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("add", ".", cwd=root)
    _git("commit", "-m", "init", cwd=root)
    return root


def _request(workspace: Path, command: str) -> GateRunRequest:
    return GateRunRequest(
        routine_id="rt-interpreter",
        run_id="run-interpreter",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": command, "expected_exit_code": 0},
        workspace=workspace,
    )


@pytest.fixture
def store(tmp_path: Path) -> GateEvidenceStore:
    return GateEvidenceStore(tmp_path / "gate-evidence")


@pytest.fixture(scope="module")
def pinned_repo_workspace() -> Iterator[Path]:
    """A CLEAN checkout of this repo at HEAD, which is what a gate workspace is.

    The runner refuses a dirty workspace (correctly — a moving tree cannot pin
    evidence), and a developer's checkout is dirty most of the time. A detached
    worktree at HEAD is clean by construction, so this test asserts the same
    property in a working copy and in CI.
    """
    with tempfile.TemporaryDirectory(prefix="gate-interpreter-pin-") as parent:
        tree = Path(parent) / "pin"
        add = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "worktree", "add", "--detach", str(tree), "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert add.returncode == 0, f"could not pin a workspace: {add.stderr or add.stdout}"
        try:
            yield tree
        finally:
            subprocess.run(
                ["git", "-C", str(REPO_ROOT), "worktree", "remove", "--force", str(tree)],
                capture_output=True,
                text=True,
                check=False,
            )


# ---------------------------------------------------------------- derivation


def test_a_target_under_loops_selects_the_loops_interpreter(tmp_path: Path) -> None:
    workspace = _synthetic_workspace(tmp_path)
    assert (
        interpreter_class_for_targets(workspace, ("loops/tests/test_loopy.py",))
        == INTERPRETER_CLASS_LOOPS
    )


def test_a_production_target_selects_the_repo_interpreter(tmp_path: Path) -> None:
    workspace = _synthetic_workspace(tmp_path)
    assert (
        interpreter_class_for_targets(workspace, ("tests/scheduler/test_prod.py",))
        == INTERPRETER_CLASS_REPO
    )


def test_a_node_id_selector_does_not_change_the_class(tmp_path: Path) -> None:
    workspace = _synthetic_workspace(tmp_path)
    assert (
        interpreter_class_for_targets(workspace, ("loops/tests/test_loopy.py::test_one",))
        == INTERPRETER_CLASS_LOOPS
    )


def test_a_gate_mixing_loops_and_production_targets_is_refused(tmp_path: Path) -> None:
    """No interpreter can run both, so there is no winner to pick — only a refusal."""
    workspace = _synthetic_workspace(tmp_path)
    with pytest.raises(GateEvidenceRefusal, match="different interpreters"):
        interpreter_class_for_targets(
            workspace, ("loops/tests/test_loopy.py", "tests/scheduler/test_prod.py")
        )


def test_the_class_follows_the_real_path_not_the_spelling(tmp_path: Path) -> None:
    """A symlink named ``loops/...`` that points at production code is production.

    The class is derived from the containment-checked, symlink-resolved parts —
    the same data ``resolve_targets`` already validates — so the spelling of a
    target cannot pick the interpreter for a file that lives somewhere else.
    """
    workspace = _synthetic_workspace(tmp_path)
    (workspace / "loops" / "tests" / "linked_prod.py").symlink_to(
        workspace / "tests" / "scheduler" / "test_prod.py"
    )
    assert (
        interpreter_class_for_targets(workspace, ("loops/tests/linked_prod.py",))
        == INTERPRETER_CLASS_REPO
    )


def test_the_runner_executes_a_loops_target_with_the_loops_interpreter(
    tmp_path: Path, store: GateEvidenceStore
) -> None:
    """argv[0] is the loops interpreter, and the receipt says so.

    The production interpreter is deliberately set to a path that does not exist,
    so this suite can only go green if the runner really executed argv[0] from
    the LOOPS slot. ``sys.executable`` stands in for the loops venv because a
    second working venv is not something a unit test should have to build; the
    end-to-end test below uses the real one.
    """
    workspace = _synthetic_workspace(tmp_path)

    evidence = PytestGateRunner(
        store,
        python_executable=str(tmp_path / "no-such-prod-venv" / "bin" / "python"),
        loops_python_executable=sys.executable,
    ).run(_request(workspace, "pytest loops/tests/test_loopy.py"))

    assert evidence.exit_code == 0
    assert evidence.checks_passed == 1
    assert Path(evidence.interpreter).name == Path(sys.executable).name
    assert Path(evidence.interpreter).parent == Path(sys.executable).parent.resolve()
    if Path(sys.executable).is_symlink():
        # The venv identity survived: a `Path.resolve()` on an interpreter path
        # records the shared base interpreter, which is byte-identical for the
        # production and the loops venv — it would delete the one field in the
        # receipt that says which dependency closure produced the verdict.
        assert evidence.interpreter != str(Path(sys.executable).resolve())


def test_a_missing_loops_interpreter_is_absence_not_a_failed_gate(
    tmp_path: Path, store: GateEvidenceStore
) -> None:
    """A host with no loops venv judged nothing; it must not settle gate_passed=0.

    ``unavailable`` settles NULL/NULL and stays out of the acceptance floor.
    ``refused`` would settle 0 and count toward auto-pause — recreating the
    false-adverse defect this lane closes, one layer down.
    """
    workspace = _synthetic_workspace(tmp_path)
    runner = PytestGateRunner(
        store, loops_python_executable=tmp_path / "no-such-venv" / "bin" / "python"
    )
    request = _request(workspace, "pytest loops/tests/test_loopy.py")

    with pytest.raises(GateWorkspaceUnusable, match="loops interpreter is missing"):
        runner.run(request)

    outcome = produce_gate_evidence(runner, store, request)
    assert outcome.status == "unavailable"
    assert "loops interpreter is missing" in outcome.detail


def test_a_production_gate_still_runs_on_the_production_interpreter(
    tmp_path: Path, store: GateEvidenceStore
) -> None:
    workspace = _synthetic_workspace(tmp_path)
    evidence = PytestGateRunner(
        store, loops_python_executable=tmp_path / "no-such-venv" / "bin" / "python"
    ).run(_request(workspace, "pytest tests/scheduler/test_prod.py"))

    assert evidence.exit_code == 0
    assert Path(evidence.interpreter).name == Path(sys.executable).name
    assert Path(evidence.interpreter).parent == Path(sys.executable).parent.resolve()


def test_the_loops_interpreter_default_matches_the_loop_worker_rule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same precedence as ``loops/bin/loop-worker`` and ``paths.venv_python``.

    The gate must grade a loop instance's tests on the runtime that instance
    actually runs on. Two rules would mean the worker on venv X and its evidence
    from venv Y.
    """
    monkeypatch.setenv("OMNIAGENTOS_LOOPS_VENV", str(tmp_path / "explicit"))
    monkeypatch.setenv("OMNIAGENTOS_LOOPS_ROOT", str(tmp_path / "root"))
    assert default_loops_interpreter() == tmp_path / "explicit" / "bin" / "python"

    monkeypatch.delenv("OMNIAGENTOS_LOOPS_VENV")
    assert default_loops_interpreter() == tmp_path / "root" / "venv" / "bin" / "python"

    monkeypatch.delenv("OMNIAGENTOS_LOOPS_ROOT")
    assert default_loops_interpreter() == REPO_ROOT / "var" / "loops" / "venv" / "bin" / "python"


# ------------------------------------------------------- executed, not resolved


@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.skipif(
    not os.access(default_loops_interpreter(), os.X_OK),
    reason=(
        "no loops venv on this host (build it with loops/requirements.txt); "
        "loops/bin/loop-tests exits 127 under the same condition"
    ),
)
def test_a_loop_gate_naming_its_own_tests_passes_in_the_gate_runner(
    pinned_repo_workspace: Path, store: GateEvidenceStore
) -> None:
    """The regression test for the incident: the live W3 gate, really executed.

    Before the interpreter was derived from the target path this command settled
    ``9 failed, 52 passed`` with ``ModuleNotFoundError: No module named
    'langgraph'`` — a gate that could never pass, on a suite that was green all
    along.
    """
    evidence = PytestGateRunner(store).run(_request(pinned_repo_workspace, LOOPS_GATE_COMMAND))

    assert evidence.exit_code == 0, f"loops gate did not pass: {evidence}"
    assert evidence.checks_failed == 0
    assert evidence.checks_collected > 0
    assert evidence.checks_passed == evidence.checks_collected
    expected = default_loops_interpreter()
    assert Path(evidence.interpreter).name == expected.name
    assert Path(evidence.interpreter).parent == expected.parent.resolve()
    assert evidence.interpreter != str(Path(sys.executable).resolve())
    assert evidence.workspace_tree_clean is True
    assert store.verify(evidence)
