"""Fail-closed formation verifier behavior at the disabled-gate boundary."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from omniagentos.gates.engine import GateResult, GateSpec
from omniagentos.swarm.scheduler import default_verifier

REFUSAL = "mechanical gate disabled and no verify_command — refusing vacuous pass."


@pytest.mark.parametrize("gate_value", [False, "false", "0", "no", "False", "No"])
def test_formation_gate_disabled_vacuous_pass_refused(
    tmp_path: Path, gate_value: bool | str
) -> None:
    ok, message = default_verifier(
        {},
        {"formation_mechanical_gate": gate_value, "verify_command": ""},
        str(tmp_path),
    )

    assert not ok, (
        f"Expected default_verifier to fail when gate is {gate_value!r} "
        "and no command is provided"
    )
    assert message == REFUSAL


def _gate_result(*, ok: bool, infra_error: str | None = None) -> GateResult:
    return GateResult(
        name="formation-test",
        command="git diff --check",
        ok=ok,
        exit_code=0 if ok else 1,
        output="focused gate output",
        duration_ms=1.0,
        blocking=True,
        infra_error=infra_error,
    )


@pytest.mark.parametrize(
    ("result", "expected_ok", "expected_message"),
    [
        (_gate_result(ok=True), True, "focused gate output"),
        (_gate_result(ok=False), False, "focused gate output"),
        (
            _gate_result(ok=False, infra_error="mechanical infrastructure failed"),
            False,
            "mechanical infrastructure failed",
        ),
    ],
)
def test_disabled_gate_runs_explicit_command_and_preserves_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: GateResult,
    expected_ok: bool,
    expected_message: str,
) -> None:
    def fake_run_gates(
        specs: Sequence[GateSpec], working_dir: str
    ) -> list[GateResult]:
        assert len(specs) == 1
        assert specs[0].argv == ("git", "diff", "--check")
        assert working_dir == str(tmp_path)
        return [result]

    monkeypatch.setattr("omniagentos.gates.engine.run_gates", fake_run_gates)

    ok, message = default_verifier(
        {},
        {
            "formation_mechanical_gate": False,
            "verify_command": "git diff --check",
        },
        str(tmp_path),
    )

    assert ok is expected_ok
    assert expected_message in message


def test_disabled_gate_refuses_unsafe_explicit_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_run(*_args: object, **_kwargs: object) -> list[GateResult]:
        pytest.fail("unsafe command reached the gate engine")

    monkeypatch.setattr("omniagentos.gates.engine.run_gates", unexpected_run)

    ok, message = default_verifier(
        {},
        {
            "formation_mechanical_gate": False,
            "verify_command": "git diff --check; touch PWNED",
        },
        str(tmp_path),
    )

    assert ok is False
    assert message.startswith("unsafe verifier command refused:")


def test_disabled_gate_refuses_invalid_suite_shape(tmp_path: Path) -> None:
    ok, message = default_verifier(
        {},
        {
            "formation_mechanical_gate": False,
            "verify_command": "git diff --check",
            "mechanical_suite_commands": "pytest -q",
        },
        str(tmp_path),
    )

    assert ok is False
    assert message == "mechanical_suite_commands must be a list of verifier commands"


def test_disabled_gate_preserves_import_shadow_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omniagentos.swarm.scheduler.assert_touched_modules_importable",
        lambda _working_dir, _touched: (False, "owned.py resolves to its package"),
    )

    ok, message = default_verifier(
        {},
        {
            "formation_mechanical_gate": False,
            "verify_command": "git diff --check",
            "touched_paths": ["owned.py"],
        },
        str(tmp_path),
    )

    assert ok is False
    assert message == "import-shadow check failed: owned.py resolves to its package"
