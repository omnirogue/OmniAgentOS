"""Static hygiene installer checks complement the fake-destination behavioral
coverage in ``tests/archdocs/test_operations_truth.py``.  The installer is
render-only and must never invoke live launchd."""

from __future__ import annotations

from pathlib import Path

_INSTALL_SH = Path("scripts/hygiene/install-hygiene.sh")
_JOB_SH = Path("scripts/hygiene/hygiene.sh")


def test_install_sh_is_executable() -> None:
    mode = _INSTALL_SH.stat().st_mode
    assert mode & 0o111, "scripts/hygiene/install-hygiene.sh must be installed executable"


def test_job_sh_is_executable() -> None:
    mode = _JOB_SH.stat().st_mode
    assert mode & 0o111, "scripts/hygiene/hygiene.sh must be installed executable"


def test_install_sh_resolves_the_venv_python_first() -> None:
    content = _INSTALL_SH.read_text()
    assert ".venv/bin/python" in content
    assert "python3.12" in content


def test_job_sh_resolves_the_venv_python_first() -> None:
    content = _JOB_SH.read_text()
    assert ".venv/bin/python" in content
    assert "python3.12" in content


def test_job_sh_invokes_hygiene_py_via_the_resolved_interpreter() -> None:
    content = _JOB_SH.read_text()
    assert '"$PYBIN" "$SCRIPT_DIR/hygiene.py"' in content


def test_install_sh_sources_connections_env_with_set_dash_a() -> None:
    """`set -a; . connections.env; set +a` -- a bare `. file` leaves the
    sourced vars shell-local (the recurring gotcha every installer in this
    repo guards against)."""
    content = _INSTALL_SH.read_text()
    assert 'set -a; . "$HOME/.config/omni/connections.env"' in content
    assert "set +a" in content


def test_install_sh_schedules_a_single_daily_run_at_0415_by_default() -> None:
    content = _INSTALL_SH.read_text()
    assert "HOUR_DEFAULT" not in content  # sanity: no leftover placeholder naming
    assert "OMNIAGENTOS_HYGIENE_HOUR:-4" in content
    assert "OMNIAGENTOS_HYGIENE_MINUTE:-15" in content


def test_install_sh_never_invokes_live_launchctl() -> None:
    content = _INSTALL_SH.read_text()
    assert "\nlaunchctl unload" not in content
    assert "\nlaunchctl load" not in content
    assert "NOT loaded" in content


def test_install_sh_uses_the_render_only_launchd_helper() -> None:
    content = _INSTALL_SH.read_text()
    # ba7172d41 made this a package-qualified import after the bare relative
    # import crashed inside launchd.py's own relative import.
    assert "from scripts.hygiene.launchd import render_template" in content


def test_install_sh_lints_the_rendered_plist() -> None:
    content = _INSTALL_SH.read_text()
    assert "plutil -lint" in content


def test_job_sh_redirects_all_output_to_hygiene_log() -> None:
    content = _JOB_SH.read_text()
    assert 'LOG_FILE="$ROOT_DIR/var/log/hygiene.log"' in content
    assert 'exec >>"$LOG_FILE" 2>&1' in content
