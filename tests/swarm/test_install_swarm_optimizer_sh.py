"""install-swarm-optimizer.sh's shell logic itself is not unit-testable
(mirrors the curator/H1-scheduler precedent: see tests/lab/curator/test_run_sh.py's
own docstring — "test the python templating helper, not an actual launchctl
load"). These tests instead assert on its static content, and leave
`scripts/swarm/launchd.py`'s pure Python renderer (tests/swarm/test_launchd.py)
as the actually-executed coverage. This suite NEVER shells out to the script
itself — running it would really call `launchctl load`, a real deploy action
this WP is explicitly scoped OUT of ("Do NOT install/load the job").
"""

from __future__ import annotations

from pathlib import Path

_INSTALL_SH = Path("scripts/swarm/install-swarm-optimizer.sh")


def test_install_sh_is_executable() -> None:
    mode = _INSTALL_SH.stat().st_mode
    assert mode & 0o111, "scripts/swarm/install-swarm-optimizer.sh must be installed executable"


def test_install_sh_resolves_the_venv_python_first() -> None:
    content = _INSTALL_SH.read_text()
    assert ".venv/bin/python" in content
    assert "python3.12" in content


def test_install_sh_invokes_the_optimizer_module_via_the_resolved_interpreter() -> None:
    content = _INSTALL_SH.read_text()
    assert "omniagentos.swarm.optimize" in content
    assert "PYBIN" in content


def test_install_sh_sources_connections_env_with_set_dash_a() -> None:
    """`set -a; . connections.env; set +a` -- a bare `. file` leaves the
    sourced vars shell-local (the recurring gotcha every other installer in
    this repo guards against; see docs/architecture/scheduling.md)."""
    content = _INSTALL_SH.read_text()
    assert 'set -a; . "$HOME/.config/omni/connections.env"' in content
    assert "set +a" in content


def test_install_sh_schedules_two_daily_runs_staggered_off_the_curator() -> None:
    content = _INSTALL_SH.read_text()
    assert "HOUR1" in content
    assert "HOUR2" in content
    # Default 3:45/15:45 -- 15 minutes off the selfimprove-curator's 3:30/15:30.
    assert "HOUR_1:-3" in content
    assert "MINUTE_1:-45" in content
    assert "HOUR_2:-15" in content
    assert "MINUTE_2:-45" in content


def test_install_sh_is_idempotent_reload_not_append() -> None:
    content = _INSTALL_SH.read_text()
    assert "launchctl unload" in content
    assert "launchctl load" in content


def test_install_sh_uses_the_render_only_launchd_helper() -> None:
    content = _INSTALL_SH.read_text()
    assert "from launchd import render_template" in content


def test_install_sh_never_writes_to_swarm_config() -> None:
    """WP8 hard invariant: recommendations are human-applied, never
    auto-edited into config. The install script may only ever WRITE the
    rendered plist -- it must contain no redirect/sed/write targeting
    configs/swarm.yaml (mentioning the invariant in a comment is fine; writing
    to the file is not)."""
    content = _INSTALL_SH.read_text()
    for line in content.splitlines():
        assert "swarm.yaml" not in line or line.strip().startswith("#")
