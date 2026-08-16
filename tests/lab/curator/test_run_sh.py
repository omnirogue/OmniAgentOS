"""Static curator installer checks complement the fake-destination behavioral
coverage in ``tests/archdocs/test_operations_truth.py``.  The installer only
renders a plist and never invokes live launchd."""

from __future__ import annotations

from pathlib import Path

_RUN_SH = Path("scripts/curator/run.sh")


def test_run_sh_is_executable() -> None:
    mode = _RUN_SH.stat().st_mode
    assert mode & 0o111, "scripts/curator/run.sh must be installed executable"


def test_run_sh_resolves_the_venv_python_like_the_h1_scheduler() -> None:
    content = _RUN_SH.read_text()
    assert ".venv/bin/python" in content
    assert "python3.12" in content


def test_run_sh_invokes_the_curator_module_via_the_resolved_interpreter() -> None:
    content = _RUN_SH.read_text()
    assert "omniagentos.lab.curator" in content
    assert "PYBIN" in content


def test_run_sh_scrubs_the_protected_env_var_before_installing_the_job() -> None:
    content = _RUN_SH.read_text()
    assert "OMNIAGENTOS_EVAL_PROTECTED" in content
    assert "unset OMNIAGENTOS_EVAL_PROTECTED" in content


def test_run_sh_schedules_two_daily_runs() -> None:
    content = _RUN_SH.read_text()
    assert "HOUR1" in content
    assert "HOUR2" in content


def test_run_sh_never_invokes_live_launchctl() -> None:
    content = _RUN_SH.read_text()
    assert "\nlaunchctl unload" not in content
    assert "\nlaunchctl load" not in content
    assert "NOT loaded" in content
