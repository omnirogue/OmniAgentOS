import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from omniagentos.gates.engine import GateSpec, run_gates
from omniagentos.taskcontract.models import AcceptanceCriterion, Gate, RiskClass, TaskContract


def test_run_gates_passing(tmp_path: Path):
    specs = [GateSpec(argv=["echo", "hello world"], name="pass_echo")]
    results = run_gates(specs, str(tmp_path))
    assert len(results) == 1
    res = results[0]
    assert res.name == "pass_echo"
    assert res.command == "echo 'hello world'"
    assert res.ok is True
    assert res.exit_code == 0
    assert "hello world" in res.output
    assert res.duration_ms >= 0.0
    assert res.blocking is True
    assert res.infra_error is None


def test_legacy_string_command_is_fail_closed() -> None:
    """No caller can recover the removed shell-string execution rail."""
    with pytest.raises(ValueError, match="shell execution is disabled"):
        GateSpec(command="echo legacy-not-allowed", name="legacy")


def test_argv_uses_shell_false_and_literal_metachar(tmp_path: Path):
    """Metacharacters in argv items stay data; no second shell command runs."""
    sentinel = tmp_path / "SENTINEL_SHOULD_NOT_EXIST"
    # If shell-interpreted, `touch` would create the sentinel after `;`.
    metachar = f"hello; touch {sentinel}"
    specs = [
        GateSpec(
            argv=[sys.executable, "-c", "import sys; print(sys.argv[1])", metachar],
            name="argv_meta",
        )
    ]
    observed: dict = {}

    real_run = __import__("subprocess").run

    def _spy(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return real_run(*args, **kwargs)

    with patch("omniagentos.gates.engine.subprocess.run", side_effect=_spy):
        results = run_gates(specs, str(tmp_path))

    assert results[0].ok is True
    assert metachar in results[0].output
    assert not sentinel.exists(), "shell metachar must not create sentinel"
    assert observed["kwargs"].get("shell") is False
    assert isinstance(observed["args"][0], list)
    assert observed["args"][0][3] == metachar


@pytest.mark.parametrize("scalar", ["echo hi", b"echo hi"])
def test_argv_scalar_string_refused(scalar):
    """Passing a scalar str where argv is required must not pick the safe branch."""
    with pytest.raises(TypeError, match="non-string sequence"):
        GateSpec(argv=scalar)  # type: ignore[arg-type]


def test_argv_empty_or_non_string_item_refused():
    with pytest.raises(ValueError, match="non-empty"):
        GateSpec(argv=[])
    with pytest.raises(TypeError, match="items must be str"):
        GateSpec(argv=[sys.executable, 7])  # type: ignore[list-item]


def test_argv_is_the_only_execution_form():
    with pytest.raises(TypeError, match="non-string sequence"):
        GateSpec()
    with pytest.raises(ValueError, match="shell execution is disabled"):
        GateSpec(command="echo x", argv=["echo", "x"])


def test_argv_display_command_no_shell_join(tmp_path: Path):
    specs = [GateSpec(argv=["echo", "a b"], name="disp")]
    results = run_gates(specs, str(tmp_path))
    # display uses shlex.join — evidence text is non-empty and not shell-run.
    assert results[0].command
    assert "a b" in results[0].command or "a\\ b" in results[0].command


def test_run_gates_failing(tmp_path: Path):
    specs = [GateSpec(argv=[sys.executable, "-c", "raise SystemExit(42)"], name="fail_exit")]
    results = run_gates(specs, str(tmp_path))
    assert len(results) == 1
    res = results[0]
    assert res.name == "fail_exit"
    assert res.ok is False
    assert res.exit_code == 42
    assert res.blocking is True
    assert res.infra_error is None


def test_run_gates_expected_exit(tmp_path: Path):
    specs = [
        GateSpec(
            argv=[sys.executable, "-c", "raise SystemExit(1)"],
            expected_exit=1,
            name="expect_one",
        )
    ]
    results = run_gates(specs, str(tmp_path))
    assert len(results) == 1
    res = results[0]
    assert res.name == "expect_one"
    assert res.ok is True
    assert res.exit_code == 1


def test_run_gates_non_blocking(tmp_path: Path):
    specs = [
        GateSpec(
            argv=[sys.executable, "-c", "raise SystemExit(5)"],
            blocking=False,
            name="non_block",
        )
    ]
    results = run_gates(specs, str(tmp_path))
    assert len(results) == 1
    res = results[0]
    assert res.name == "non_block"
    assert res.ok is False
    assert res.exit_code == 5
    assert res.blocking is False


def test_run_gates_infra_error(tmp_path: Path):
    with patch("subprocess.run", side_effect=OSError("Permission denied")):
        specs = [GateSpec(argv=["echo"], name="mocked_err")]
        results = run_gates(specs, str(tmp_path))
        assert len(results) == 1
        res = results[0]
        assert res.ok is False
        assert "Permission denied" in res.infra_error
        assert res.infra_error.startswith("mechanical command could not run:")


def test_run_gates_no_short_circuit(tmp_path: Path):
    specs = [
        GateSpec(argv=[sys.executable, "-c", "raise SystemExit(1)"], name="gate1"),
        GateSpec(argv=["echo", "gate2"], name="gate2"),
    ]
    results = run_gates(specs, str(tmp_path))
    assert len(results) == 2
    assert results[0].name == "gate1"
    assert results[0].ok is False
    assert results[1].name == "gate2"
    assert results[1].ok is True
    assert "gate2" in results[1].output


def test_task_contract_with_gates():
    # Verify contract serialization / deserialization / empty-omit stability
    criteria = (AcceptanceCriterion(id="c1", condition="Always passes"),)

    # Empty gates
    contract_empty = TaskContract(
        objective="Do work",
        acceptance_criteria=criteria,
        read_set=(),
        write_set=(),
        risk_class=RiskClass.R1,
    )
    data_empty = contract_empty.to_dict()
    assert "gates" not in data_empty

    # Deserializing empty
    parsed_empty = TaskContract.from_dict(data_empty)
    assert parsed_empty.gates == ()

    # With gates
    g1 = Gate(
        command="pytest",
        cwd="src",
        timeout_s=100,
        expected_exit=0,
        thresholds={"coverage": 90.0},
        blocking=True,
    )
    contract_with = TaskContract(
        objective="Do work",
        acceptance_criteria=criteria,
        read_set=(),
        write_set=(),
        risk_class=RiskClass.R1,
        gates=(g1,),
    )
    data_with = contract_with.to_dict()
    assert "gates" in data_with
    assert len(data_with["gates"]) == 1
    assert data_with["gates"][0]["command"] == "pytest"
    assert data_with["gates"][0]["cwd"] == "src"
    assert data_with["gates"][0]["timeout_s"] == 100
    assert data_with["gates"][0]["expected_exit"] == 0
    assert data_with["gates"][0]["thresholds"] == {"coverage": 90.0}
    assert data_with["gates"][0]["blocking"] is True

    # Deserializing with gates
    parsed_with = TaskContract.from_dict(data_with)
    assert len(parsed_with.gates) == 1
    parsed_gate = parsed_with.gates[0]
    assert parsed_gate.command == "pytest"
    assert parsed_gate.cwd == "src"
    assert parsed_gate.timeout_s == 100
    assert parsed_gate.expected_exit == 0
    assert parsed_gate.thresholds == {"coverage": 90.0}
    assert parsed_gate.blocking is True
