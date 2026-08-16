"""Tests for the mechanical gate scripts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from omniagentos.gates import planner_canary

ROOT = Path(__file__).resolve().parents[2]
GATES = ROOT / "scripts" / "gates"


def test_shell_scripts_have_valid_syntax() -> None:
    for script in (GATES / "mech_gate.sh", GATES / "agent_watchdog.sh"):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_planner_canary_success_appends_json(tmp_path: Path, capsys: object) -> None:
    log_path = tmp_path / "var" / "log" / "planner-canary.jsonl"
    with (
        mock.patch.object(planner_canary, "_log_path", return_value=log_path),
        mock.patch.object(planner_canary, "run_fable_json", return_value={"ok": True}),
    ):
        assert planner_canary.main(now=datetime(2026, 1, 1, 10, tzinfo=UTC)) == 0
    assert "CANARY OK" in capsys.readouterr().out  # type: ignore[union-attr]
    entry = json.loads(log_path.read_text().strip())
    assert entry["ok"] is True
    assert isinstance(entry["ms"], int)


def test_planner_canary_none_reports_failure(tmp_path: Path, capsys: object) -> None:
    log_path = tmp_path / "var" / "log" / "planner-canary.jsonl"
    with (
        mock.patch.object(planner_canary, "_log_path", return_value=log_path),
        mock.patch.object(planner_canary, "run_fable_json", return_value=None),
    ):
        assert planner_canary.main(now=datetime(2026, 1, 1, 10, tzinfo=UTC)) == 1
    stderr = capsys.readouterr().err  # type: ignore[union-attr]
    # Assert semantic parts of the failure message rather than a brittle literal.
    # The message includes the model name and consequence, but the durable invariants are:
    # - CANARY FAILED marker
    # - planner mentioned
    # - returned None documented
    assert "CANARY FAILED:" in stderr
    assert "planner" in stderr
    assert "returned None" in stderr
    assert json.loads(log_path.read_text())["ok"] is False


def test_baselines_are_single_integers() -> None:
    for name in ("ruff-baseline.txt", "mypy-baseline.txt"):
        value = (GATES / name).read_text().strip()
        assert value.isdigit()


MECH_GATE = GATES / "mech_gate.sh"

# Stub interpreter for hermetic gate tests: it answers the `-m ruff` / `-m
# mypy` module probes from environment variables and delegates anything else
# (e.g. `-c ...`) to the real interpreter that launched it.
STUB_INTERPRETER = """\
#!__PYTHON__
import os
import sys

args = sys.argv[1:]
log = os.environ.get("MECH_GATE_STUB_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(" ".join(args) + "\\n")
if args[:2] == ["-m", "ruff"]:
    if os.environ.get("MECH_GATE_NO_RUFF"):
        sys.stderr.write("No module named ruff\\n")
        sys.exit(1)
    if len(args) > 2 and args[2] == "--version":
        print("ruff 0.0.0")
        sys.exit(0)
    print(os.environ.get("MECH_GATE_RUFF_JSON", "[]"))
    sys.exit(int(os.environ.get("MECH_GATE_RUFF_RC", "0")))
if args[:2] == ["-m", "mypy"]:
    if os.environ.get("MECH_GATE_NO_MYPY"):
        sys.stderr.write("No module named mypy\\n")
        sys.exit(1)
    if len(args) > 2 and args[2] == "--version":
        print("mypy 1.0.0 (compiled: yes)")
        sys.exit(0)
    sys.stdout.write(os.environ.get("MECH_GATE_MYPY_OUTPUT", ""))
    sys.exit(int(os.environ.get("MECH_GATE_MYPY_RC", "0")))
if args[:2] == ["-m", "pytest"]:
    if os.environ.get("MECH_GATE_NO_PYTEST"):
        sys.stderr.write("No module named pytest\\n")
        sys.exit(1)
    sys.exit(int(os.environ.get("MECH_GATE_PYTEST_RC", "0")))
os.execv(sys.executable, [sys.executable, *args])
"""

# Two error diagnostics buried in three notes: 2 errors, 5 output lines total.
MYPY_OUTPUT_WITH_NOTES = (
    "omniagentos/a.py:10:5: error: Incompatible types in assignment [assignment]\n"
    "omniagentos/a.py:10:5: note: Revealed type is 'builtins.int'\n"
    "omniagentos/a.py:11:5: note: See https://mypy.readthedocs.io/\n"
    "omniagentos/b.py:20: error: Missing return statement [empty-body]\n"
    "omniagentos/b.py:21: note: Perhaps add an explicit 'return None'\n"
)


def _mech_gate_tree(tmp_path: Path, *, ruff_baseline: int = 0, mypy_baseline: int = 0) -> Path:
    gates = tmp_path / "scripts" / "gates"
    gates.mkdir(parents=True)
    shutil.copy(MECH_GATE, gates / "mech_gate.sh")
    (gates / "ruff-baseline.txt").write_text(f"{ruff_baseline}\n")
    (gates / "mypy-baseline.txt").write_text(f"{mypy_baseline}\n")
    return gates / "mech_gate.sh"


def _run_mech_gate(
    tmp_path: Path, script: Path, **env_overrides: str
) -> subprocess.CompletedProcess[str]:
    stub_python = tmp_path / "stub_python"
    stub_python.write_text(STUB_INTERPRETER.replace("__PYTHON__", sys.executable))
    stub_python.chmod(0o755)
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    fallback_python = venv_bin / "python"
    if not fallback_python.exists():
        fallback_python.symlink_to(stub_python)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # A failing PATH pytest proves that the gate uses the selected interpreter.
    pytest_stub = bin_dir / "pytest"
    pytest_stub.write_text("#!/bin/bash\nexit 97\n")
    pytest_stub.chmod(0o755)
    # PATH deliberately carries NO ruff/mypy executables, so all three required
    # tools can only succeed via the selected interpreter.
    env = {
        "OMNIAGENTOS_PYTHON": str(stub_python),
        "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]),
        "MECH_GATE_STUB_LOG": str(tmp_path / "invocations.log"),
    }
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(script)], cwd=tmp_path, env=env, capture_output=True, text=True
    )


def test_mech_gate_ruff_via_interpreter_when_absent_from_path(tmp_path: Path) -> None:
    script = _mech_gate_tree(tmp_path, ruff_baseline=2)
    findings = [{"code": "F401"}, {"code": "E711"}]
    result = _run_mech_gate(tmp_path, script, MECH_GATE_RUFF_JSON=json.dumps(findings))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASSED: ruff=2" in result.stdout
    # ruff ran as a module of OMNIAGENTOS_PYTHON; PATH never provided a ruff.
    assert "-m ruff check" in (tmp_path / "invocations.log").read_text()


@pytest.mark.parametrize(
    ("ruff_rc", "ruff_baseline", "findings", "expected_rc", "expected_text"),
    [
        ("1", 1, [{"code": "F401"}], 0, "PASSED: ruff=1"),
        ("2", 0, [], 1, "ruff check (999999 > 0)"),
    ],
)
def test_mech_gate_ruff_exit_status_fails_closed_on_tool_errors(
    tmp_path: Path,
    ruff_rc: str,
    ruff_baseline: int,
    findings: list[dict[str, str]],
    expected_rc: int,
    expected_text: str,
) -> None:
    script = _mech_gate_tree(tmp_path, ruff_baseline=ruff_baseline)
    result = _run_mech_gate(
        tmp_path,
        script,
        MECH_GATE_RUFF_JSON=json.dumps(findings),
        MECH_GATE_RUFF_RC=ruff_rc,
    )
    assert result.returncode == expected_rc, result.stdout + result.stderr
    assert expected_text in result.stdout


@pytest.mark.parametrize(
    ("missing_var", "tool"),
    [("MECH_GATE_NO_RUFF", "ruff"), ("MECH_GATE_NO_MYPY", "mypy")],
)
def test_mech_gate_missing_required_tool_fails_closed(
    tmp_path: Path, missing_var: str, tool: str
) -> None:
    script = _mech_gate_tree(tmp_path)
    result = _run_mech_gate(tmp_path, script, **{missing_var: "1"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "PASSED" not in result.stdout
    assert f"{tool} not importable" in result.stdout


@pytest.mark.parametrize("override_kind", ["empty", "missing", "non_executable", "not_python"])
def test_mech_gate_invalid_explicit_python_never_falls_back(
    tmp_path: Path, override_kind: str
) -> None:
    script = _mech_gate_tree(tmp_path)
    explicit_python = tmp_path / "explicit_python"
    if override_kind not in {"empty", "missing"}:
        explicit_python.write_text("#!/bin/bash\nexit 0\n")
    if override_kind == "not_python":
        explicit_python.chmod(0o755)
    override_value = "" if override_kind == "empty" else str(explicit_python)

    result = _run_mech_gate(tmp_path, script, OMNIAGENTOS_PYTHON=override_value)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "PASSED" not in result.stdout
    assert "invalid explicit OMNIAGENTOS_PYTHON" in result.stdout
    # The valid .venv fallback installed by the helper must remain untouched.
    assert not (tmp_path / "invocations.log").exists()


def test_mech_gate_pytest_uses_selected_interpreter_and_missing_fails_closed(
    tmp_path: Path,
) -> None:
    script = _mech_gate_tree(tmp_path)
    result = _run_mech_gate(tmp_path, script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "-m pytest tests/certification" in (tmp_path / "invocations.log").read_text()

    missing_result = _run_mech_gate(tmp_path, script, MECH_GATE_NO_PYTEST="1")
    assert missing_result.returncode == 1, missing_result.stdout + missing_result.stderr
    assert "PASSED" not in missing_result.stdout
    assert "pytest quick set" in missing_result.stdout
    assert "No module named pytest" in missing_result.stdout


@pytest.mark.parametrize(
    ("mypy_baseline", "expected_rc", "expected_text"),
    [
        (2, 0, "mypy=2"),  # exactly the 2 error lines count; notes are excluded
        (1, 1, "mypy check (2 > 1)"),  # the count is precisely 2, not 5 output lines
    ],
)
def test_mech_gate_mypy_counts_only_error_diagnostics(
    tmp_path: Path, mypy_baseline: int, expected_rc: int, expected_text: str
) -> None:
    script = _mech_gate_tree(tmp_path, mypy_baseline=mypy_baseline)
    result = _run_mech_gate(
        tmp_path, script, MECH_GATE_MYPY_OUTPUT=MYPY_OUTPUT_WITH_NOTES, MECH_GATE_MYPY_RC="1"
    )
    assert result.returncode == expected_rc, result.stdout + result.stderr
    assert expected_text in result.stdout


def test_mech_gate_green(tmp_path: Path) -> None:
    script = _mech_gate_tree(tmp_path)
    result = _run_mech_gate(tmp_path, script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASSED: ruff=0 mypy=0 pytest=ok" in result.stdout


def test_mechanical_gate_passes() -> None:
    # mech_gate.sh runs its own `.venv/bin/python -m pytest` quick set (the
    # DEFAULT venv, which never has testfarm installed). The A4 hermetic-lane
    # handshake flag is per-run, not inheritable: when this test itself runs
    # inside `make test-hermetic`, TESTFARM_HERMETIC=1 is set in THIS process's
    # environment, and subprocess.run() inherits it by default, turning the
    # gate's inner pytest quick set into a fail-loud handshake refusal (the
    # plugin genuinely isn't importable in .venv). Strip it — same convention
    # as tests/scripts/test_conftest_db_isolation.py.
    env = os.environ.copy()
    env.pop("TESTFARM_HERMETIC", None)
    env.pop("TESTFARM_HERMETIC_ALLOW_NETWORK_ACK", None)
    result = subprocess.run(
        ["bash", str(GATES / "mech_gate.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
