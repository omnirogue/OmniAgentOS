from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omniagentos.verify import (
    VERIFY_GATE_ENV,
    run_scoped_pytest,
    verify_mode,
    verify_syntax,
    verify_working_dir,
)


@pytest.mark.parametrize(
    "val, expected",
    [
        ("off", "off"),
        ("shadow", "shadow"),
        ("enforce", "enforce"),
        ("ENFORCE", "enforce"),
        (" enforce ", "enforce"),
        ("1", "off"),
        ("true", "off"),
        ("on", "off"),
        ("yes", "off"),
        ("", "off"),
        ("shadowy", "off"),
        ("enforce!", "off"),
    ],
)
def test_verify_mode(val: str, expected: str) -> None:
    assert verify_mode({VERIFY_GATE_ENV: val}) == expected


def test_verify_mode_unset() -> None:
    assert verify_mode({}) == "off"


def test_verify_syntax_valid(tmp_path: Path) -> None:
    f = tmp_path / "valid.py"
    f.write_text("def ok():\n    pass\n", encoding="utf-8")
    ok, detail = verify_syntax(["valid.py"], working_dir=str(tmp_path))
    assert ok is True
    assert detail == "syntax check passed"


def test_verify_syntax_broken(tmp_path: Path) -> None:
    f = tmp_path / "broken.py"
    f.write_text("def broken(:\n", encoding="utf-8")
    ok, detail = verify_syntax(["broken.py"], working_dir=str(tmp_path))
    assert ok is False
    assert "SyntaxError" in detail


def test_verify_syntax_vacuous(tmp_path: Path) -> None:
    ok, detail = verify_syntax([], working_dir=str(tmp_path))
    assert ok is False
    assert "vacuous" in detail

    f = tmp_path / "notes.txt"
    f.write_text("hello", encoding="utf-8")
    ok, detail = verify_syntax(["notes.txt"], working_dir=str(tmp_path))
    assert ok is False
    assert "vacuous" in detail


def test_run_scoped_pytest_no_tests(tmp_path: Path) -> None:
    f = tmp_path / "empty.py"
    f.write_text("def no_tests(): pass", encoding="utf-8")
    ok, detail = run_scoped_pytest(["empty.py"], working_dir=str(tmp_path))
    assert ok is False
    assert "pytest exit 5" in detail
    assert "no tests were collected" in detail


def test_run_scoped_pytest_trivial_pass(tmp_path: Path) -> None:
    f = tmp_path / "test_ok.py"
    f.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    ok, detail = run_scoped_pytest(["test_ok.py"], working_dir=str(tmp_path))
    assert ok is True
    assert "pytest passed" in detail


def test_run_scoped_pytest_vacuous() -> None:
    ok, detail = run_scoped_pytest([], working_dir=".")
    assert ok is False
    assert "vacuous" in detail


def test_verify_working_dir_untracked_broken(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    f = tmp_path / "broken.py"
    f.write_text("def bad(:\n", encoding="utf-8")

    outcome = verify_working_dir(str(tmp_path))
    assert outcome is not None
    ok, detail = outcome
    assert ok is False
    assert "SyntaxError" in detail


def test_verify_working_dir_no_changed_py(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    f = tmp_path / "notes.txt"
    f.write_text("hello", encoding="utf-8")

    outcome = verify_working_dir(str(tmp_path))
    assert outcome is None


def test_verify_working_dir_repo_with_no_commits(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), check=True)

    f = tmp_path / "broken.py"
    f.write_text("def broken(:\n", encoding="utf-8")

    outcome = verify_working_dir(str(tmp_path))
    assert outcome is not None
    ok, detail = outcome
    assert ok is False
    assert "SyntaxError" in detail
