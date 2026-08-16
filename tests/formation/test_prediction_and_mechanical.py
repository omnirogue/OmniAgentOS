"""B7 prediction formation + mechanical gate suite behavior."""

from __future__ import annotations

from pathlib import Path

from omniagentos.formation import (
    clear_formation_cache,
    select_formation_with_confidence,
    topology_for_formation,
)
from omniagentos.swarm.scheduler import _detect_mechanical_suite, default_verifier


def setup_function() -> None:
    clear_formation_cache()


def test_prediction_formation_selected_for_forecast_goal() -> None:
    sel = select_formation_with_confidence(
        goal="Build a prediction system for Globex conversion rates"
    )
    assert sel.formation.id == "prediction"
    assert sel.confidence >= 0.85
    assert topology_for_formation("prediction") == "specialist_panel"
    assert sel.formation.mechanical_gate is True


def test_mechanical_gate_false_refuses() -> None:
    ok, msg = default_verifier({}, {"formation_mechanical_gate": False}, "/tmp")
    assert ok is False
    assert msg == "mechanical gate disabled and no verify_command — refusing vacuous pass."


def test_mechanical_gate_true_runs_verify_command(tmp_path: Path) -> None:
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    ok, _msg = default_verifier(
        {},
        {
            "formation_mechanical_gate": True,
            "verify_command": "pytest -q test_ok.py",
        },
        str(tmp_path),
    )
    assert ok is True


def test_mechanical_gate_true_fails_bad_command(tmp_path: Path) -> None:
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")
    ok, msg = default_verifier(
        {},
        {
            "formation_mechanical_gate": True,
            "verify_command": "pytest -q test_bad.py",
        },
        str(tmp_path),
    )
    assert ok is False
    assert msg


def test_detect_suite_on_python_project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    cmds = _detect_mechanical_suite(str(tmp_path))
    assert any("pytest" in c for c in cmds)


def test_mechanical_gate_true_refuses_vacuous_pass(tmp_path: Path) -> None:
    ok, msg = default_verifier(
        {},
        {"formation_mechanical_gate": True},
        str(tmp_path),
    )
    assert ok is False
    assert "vacuous" in msg.lower()
