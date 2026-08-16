"""Regression coverage for connector secret diagnostics and launch loading."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from omniagentos.connectors import doctor
from omniagentos.connectors.secrets_env import load_secrets_env

ROOT = Path(__file__).resolve().parents[1]
LAUNCH_ENV = ROOT / "scripts" / "launch-env.sh"


def _registry(*names: str) -> SimpleNamespace:
    """Create the smallest registry shape needed by the doctor."""
    return SimpleNamespace(connectors={"fixture": SimpleNamespace(env=list(names))})


def test_doctor_missing_name_exits_nonzero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(doctor, "load_registry", lambda: _registry("DECLARED_MISSING"))

    assert doctor.main({}) == 1
    assert capsys.readouterr().out.splitlines() == ["DECLARED_MISSING"]


def test_doctor_all_present_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(doctor, "load_registry", lambda: _registry("FIRST_NAME", "SECOND_NAME"))

    # Empty is present for the diagnostic; the doctor intentionally ignores values.
    assert doctor.main({"FIRST_NAME": "", "SECOND_NAME": "not-inspected"}) == 0
    assert capsys.readouterr().out == ""


def test_doctor_output_is_name_only(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(doctor, "load_registry", lambda: _registry("ALPHA_TOKEN", "BETA_TOKEN"))

    assert doctor.main({}) == 1
    names = capsys.readouterr().out.splitlines()
    assert names == ["ALPHA_TOKEN", "BETA_TOKEN"]
    assert all(re.fullmatch(r"[A-Z_][A-Z_0-9]*", name) for name in names)
    assert all("=" not in name and ":" not in name and not name.startswith("sk-") for name in names)


def test_doctor_empty_vault_fails(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(doctor, "load_registry", lambda: _registry("REQUIRED_FROM_VAULT"))

    assert doctor._check_secrets({}) == ["REQUIRED_FROM_VAULT"]
    assert doctor.main({}) == 1
    assert capsys.readouterr().out == "REQUIRED_FROM_VAULT\n"


def test_doctor_reads_registry_and_environment_not_a_hardcoded_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry("FIRST_DECLARATION")
    monkeypatch.setattr(doctor, "load_registry", lambda: registry)

    assert doctor._check_secrets({}) == ["FIRST_DECLARATION"]
    registry.connectors["fixture"].env.append("SECOND_DECLARATION")
    assert doctor._check_secrets({"FIRST_DECLARATION": "present"}) == ["SECOND_DECLARATION"]


def test_doctor_module_cli_uses_registry_and_exits_with_status(tmp_path: Path) -> None:
    (tmp_path / "connectors.yaml").write_text(
        """\
version: 1
groups:
  fixture:
    label: Fixture
connectors:
  fixture:
    label: Fixture
    group: fixture
    env: [CLI_REQUIRED_NAME]
    capabilities: {}
""",
        encoding="utf-8",
    )
    env = {"OMNIAGENTOS_VAR_DIR": str(tmp_path), "PYTHONPATH": str(ROOT)}

    missing = subprocess.run(
        [sys.executable, "-m", "omniagentos.connectors.doctor"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 1
    assert missing.stdout.splitlines() == ["CLI_REQUIRED_NAME"]

    env["CLI_REQUIRED_NAME"] = ""
    present = subprocess.run(
        [sys.executable, "-m", "omniagentos.connectors.doctor"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert present.returncode == 0
    assert present.stdout == ""


def test_doctor_reports_empty_but_vault_backed_names_distinctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The loader refills blanks; the operator has to be able to see which ones.

    ``emit_export_lines`` treats an empty value as unset and restores it from the
    vault, so blanking a variable does not disable a connector — it survives
    exactly until the next launch. A doctor that reports such a name as simply
    "present" leaves the operator believing a live connector is off.
    """
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    secrets.joinpath("fixture.env").write_text("SHADOWED_NAME=vault-value\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "load_registry", lambda: _registry("SHADOWED_NAME", "REAL_NAME"))
    # _default_secrets_dir() resolves the vault as a SIBLING of the var dir.
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "runtime"))

    env = {"SHADOWED_NAME": "", "REAL_NAME": "configured"}
    assert doctor._check_empty_but_vault_backed(env, secrets_dir=str(secrets)) == ["SHADOWED_NAME"]
    # A name with a real value is not shadowed, and neither is an absent one.
    assert doctor._check_empty_but_vault_backed(
        {"SHADOWED_NAME": "set", "REAL_NAME": "configured"}, secrets_dir=str(secrets)
    ) == []

    assert doctor.main(env) == 0, "an advisory category must not change the exit status"
    out = capsys.readouterr().out.splitlines()
    assert out == [doctor.EMPTY_BUT_VAULT_BACKED_HEADER, "SHADOWED_NAME"]
    # Names only, never values — the vault query cannot return one.
    assert "vault-value" not in "\n".join(out)


def test_doctor_empty_but_vault_backed_is_silent_without_a_vault(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No vault entry means no shadowing, so the report stays byte-identical."""
    monkeypatch.setattr(doctor, "load_registry", lambda: _registry("FIRST_NAME"))
    monkeypatch.setattr(doctor, "_check_empty_but_vault_backed", lambda *_a, **_k: [])

    assert doctor.main({"FIRST_NAME": ""}) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("initial", [None, ""], ids=["absent", "empty-shadow"])
def test_empty_shadow_fix_module(tmp_path: Path, initial: str | None) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    secrets.joinpath("fixture.env").write_text("TEST_NAME=vault-value\n", encoding="utf-8")
    env: dict[str, str] = {}
    if initial is not None:
        env["TEST_NAME"] = initial

    assert load_secrets_env(secrets, environ=env) == ["TEST_NAME"]
    assert env["TEST_NAME"] == "vault-value"


def _fake_launch_repo(tmp_path: Path, secrets_module: str) -> Path:
    """Build a disposable launch-env root with a controlled secrets module."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(LAUNCH_ENV, repo / "scripts" / "launch-env.sh")
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").symlink_to(Path(sys.executable))
    package = repo / "omniagentos" / "connectors"
    package.mkdir(parents=True)
    package.parent.joinpath("__init__.py").write_text("", encoding="utf-8")
    package.joinpath("__init__.py").write_text("", encoding="utf-8")
    package.joinpath("secrets_env.py").write_text(secrets_module, encoding="utf-8")
    return repo


@pytest.mark.parametrize("initial", [None, ""], ids=["absent", "empty-shadow"])
def test_empty_shadow_fix_shell(tmp_path: Path, initial: str | None) -> None:
    secrets_module = (ROOT / "omniagentos" / "connectors" / "secrets_env.py").read_text(encoding="utf-8")
    repo = _fake_launch_repo(tmp_path, secrets_module)
    secrets = repo / "var" / "secrets"
    secrets.mkdir(parents=True)
    secrets.joinpath("fixture.env").write_text("TEST_NAME=vault-value\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("OMNIAGENTOS_LAUNCH_ENV_LOADED", None)
    env.pop("TEST_NAME", None)
    if initial is not None:
        env["TEST_NAME"] = initial

    # The probe REPORTS the resolved value and Python judges it. The previous
    # spelling ran `test "$TEST_NAME" = vault-value` in a shell with no `set -e`
    # and then `printf OK`: the exit status came from printf, so the comparison
    # could fail — or launch-env.sh could stop sourcing before ever reaching the
    # secrets load — and the test still saw returncode 0 and stdout "OK". It
    # passed against its own counterfeit.
    #
    # `set -eu` in addition, so an early abort inside the sourced file is a
    # non-zero exit rather than a silently short environment.
    result = subprocess.run(
        [
            "bash",
            "-c",
            "set -eu\n"
            f'. "{repo / "scripts" / "launch-env.sh"}"\n'
            'printf "TEST_NAME=[%s]" "${TEST_NAME-<unset>}"',
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "TEST_NAME=[vault-value]", result.stdout + result.stderr
    # The value must reach the environment without ever being echoed by the
    # loader itself; only this test's own probe is allowed to print it.
    assert "vault-value" not in result.stderr


def test_secrets_load_failure_logged(tmp_path: Path) -> None:
    repo = _fake_launch_repo(
        tmp_path,
        "def emit_export_lines(*_args, **_kwargs):\n    raise RuntimeError('fixture secrets failure')\n",
    )
    env = os.environ.copy()
    env.pop("OMNIAGENTOS_LAUNCH_ENV_LOADED", None)

    result = subprocess.run(
        ["bash", "-c", f'set -eu\n. "{repo / "scripts" / "launch-env.sh"}"\nprintf OK'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "OK"
    assert "launch-env.sh: failed to load runtime secrets" in result.stderr
    assert "fixture secrets failure" in result.stderr
