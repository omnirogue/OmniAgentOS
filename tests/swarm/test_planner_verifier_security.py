"""Release-blocking controls for the planner-to-verifier execution boundary."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from omniagentos.swarm.plan_safety import (
    VerifierCommandError,
    evaluate_plan_safety,
    parse_verifier_command,
)
from omniagentos.swarm.planner import build_plan, plan_payload
from omniagentos.swarm.scheduler import default_verifier


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("pytest tests/unit/test_widget.py", ("pytest", "tests/unit/test_widget.py")),
        (
            "python -m pytest -q tests/unit/test_widget.py::test_ok",
            ("python", "-m", "pytest", "-q", "tests/unit/test_widget.py::test_ok"),
        ),
        ("pytest -q", ("pytest", "-q", ".")),
        ("ruff check omniagentos tests", ("ruff", "check", "omniagentos", "tests")),
        ("mypy omniagentos", ("mypy", "omniagentos")),
        ("pyright dashboard", ("pyright", "dashboard")),
        ("git diff --check", ("git", "diff", "--check")),
    ],
)
def test_strict_verifier_grammar_produces_immutable_argv(
    command: str, expected: tuple[str, ...]
) -> None:
    argv = parse_verifier_command(command)
    assert argv == expected
    assert isinstance(argv, tuple)


@pytest.mark.parametrize(
    "command",
    [
        "true; touch PWNED",
        "pytest tests && true",
        "pytest tests || true",
        "pytest tests | cat",
        "pytest tests > result.txt",
        "pytest tests < input.txt",
        "pytest `printf tests`",
        "pytest $(printf tests)",
        "pytest ${TARGET}",
        "pytest tests\nruff check .",
        "pytest @targets.txt",
        "pytest /tmp/outside/test_bad.py",
        r"pytest C:\outside\test_bad.py",
        "pytest tests/../../outside/test_bad.py",
        "PYTHONPATH=. pytest tests",
        "rm -rf .",
        "pytest --collect-only tests",
        "pytest --help tests",
        "ruff check --exit-zero .",
        "mypy --config-file ../outside.ini omniagentos",
        "git diff --check HEAD",
    ],
)
def test_adversarial_verifier_commands_are_refused(command: str) -> None:
    with pytest.raises(VerifierCommandError):
        parse_verifier_command(command)


def test_shell_payload_is_non_ready_at_plan_safety_and_never_runs(tmp_path: Path) -> None:
    sentinel = tmp_path / "PWNED"
    plan = build_plan(
        "verify safely",
        [
            {
                "id": "secure",
                "title": "Secure",
                "description": "verify",
                "owned_paths": ["src/secure.py"],
                "acceptance": "verified",
                "verify_command": f"true; touch {sentinel.name}",
            }
        ],
    )
    decision = evaluate_plan_safety(plan, workspace_dir=tmp_path)
    assert decision.is_ready is False
    assert any(issue.code == "invalid_verify_command" for issue in decision.issues)
    assert not sentinel.exists()


def test_planner_persisted_task_executes_only_through_shell_false(tmp_path: Path) -> None:
    """Production build/serialize/verify path never reconstructs a shell command."""
    target = tmp_path / "tests" / "security" / "test_secure.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_secure():\n    assert True\n", encoding="utf-8")
    plan = build_plan(
        "verify safely",
        [
            {
                "id": "secure",
                "title": "Secure",
                "description": "verify",
                "owned_paths": ["src/secure.py"],
                "acceptance": "verified",
                "verify_command": "pytest -q tests/security/test_secure.py",
            }
        ],
    )
    decision = evaluate_plan_safety(plan, workspace_dir=tmp_path)
    assert decision.is_ready is True

    persisted_path = tmp_path / "plan.json"
    persisted_path.write_text(json.dumps(plan_payload(plan)), encoding="utf-8")
    persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
    persisted_task = persisted["tasks"][0]

    observed: dict[str, object] = {}

    def _spy(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "1 passed\n", "")

    with patch("omniagentos.gates.engine.subprocess.run", side_effect=_spy):
        ok, output = default_verifier({}, persisted_task, str(tmp_path))

    assert ok is True
    assert "$ pytest -q tests/security/test_secure.py" in output
    assert observed["argv"] == ["pytest", "-q", "tests/security/test_secure.py"]
    kwargs = observed["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False


def test_missing_verifier_target_is_non_ready(tmp_path: Path) -> None:
    plan = build_plan(
        "verify existing code",
        [
            {
                "id": "secure",
                "title": "Secure",
                "description": "verify",
                "owned_paths": ["src/secure.py"],
                "acceptance": "verified",
                "verify_command": "pytest tests/missing/test_missing.py",
            }
        ],
    )
    decision = evaluate_plan_safety(plan, workspace_dir=tmp_path)
    assert decision.is_ready is False
    assert any(issue.code == "verify_target_missing" for issue in decision.issues)


def test_symlinked_outside_target_is_non_ready_and_never_executes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    (workspace / "tests").mkdir(parents=True)
    outside.mkdir()
    sentinel = outside / "OUTSIDE_EXECUTED"
    (outside / "test_outside.py").write_text(
        "from pathlib import Path\n"
        "def test_outside():\n"
        f"    Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (workspace / "tests" / "linked").symlink_to(outside, target_is_directory=True)

    command = "pytest -q tests/linked/test_outside.py"
    plan = build_plan(
        "verify safely",
        [
            {
                "id": "secure",
                "title": "Secure",
                "description": "verify",
                "owned_paths": ["src/secure.py"],
                "acceptance": "verified",
                "verify_command": command,
            }
        ],
    )
    decision = evaluate_plan_safety(plan, workspace_dir=workspace)
    assert decision.is_ready is False
    assert any(issue.code == "verify_outside_workspace" for issue in decision.issues)

    ok, output = default_verifier({}, {"verify_command": command}, str(workspace))
    assert ok is False
    assert "resolves outside the workspace" in output
    assert not sentinel.exists()


def test_mutation_restoring_planner_string_gate_is_detected() -> None:
    scheduler_source = Path("omniagentos/swarm/scheduler.py").read_text(encoding="utf-8")
    engine_source = Path("omniagentos/gates/engine.py").read_text(encoding="utf-8")
    safety_source = Path("omniagentos/swarm/plan_safety.py").read_text(encoding="utf-8")
    assert "GateSpec(argv=argv, timeout_s=600)" in scheduler_source
    assert "validate_verifier_targets(argv, working_dir)" in scheduler_source
    assert "validate_verifier_targets(argv, workspace_dir)" in safety_source
    assert "GateSpec(command=cmd" not in scheduler_source
    assert "shell=True" not in engine_source
