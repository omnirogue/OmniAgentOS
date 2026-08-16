"""scripts/swarm/launchd.py — render-not-load launchd template for the WP8
swarm optimizer (mirrors tests/selfimprove/test_launchd.py's shape)."""

from __future__ import annotations

from pathlib import Path

from scripts.swarm.launchd import render_template

_TEMPLATE_PATH = Path("scripts/swarm/com.omniagentos.swarm-optimizer.plist.template")


def test_template_fills_swarm_optimizer_launchd_values_for_two_daily_runs() -> None:
    template = _TEMPLATE_PATH.read_text()
    rendered = render_template(
        template,
        label="com.example.swarm-optimizer",
        program_args=["/usr/bin/python3", "-m", "omniagentos.swarm.optimize"],
        working_dir="/tmp/omni agent",
        hour1=3,
        minute1=45,
        hour2=15,
        minute2=45,
    )
    assert "{{" not in rendered
    assert "<string>com.example.swarm-optimizer</string>" in rendered
    assert "<string>/usr/bin/python3</string>" in rendered
    assert "<string>-m</string>" in rendered
    assert "<string>omniagentos.swarm.optimize</string>" in rendered
    assert "<string>/tmp/omni agent</string>" in rendered
    assert "<integer>3</integer>" in rendered
    assert "<integer>45</integer>" in rendered
    assert "<integer>15</integer>" in rendered
    # two StartCalendarInterval entries -> 2x/day, staggered off the
    # selfimprove-curator's 3:30/15:30 slots (3:45/15:45 by convention).
    assert rendered.count("<key>Hour</key>") == 2
    assert rendered.count("<key>Minute</key>") == 2


def test_rendered_plist_escapes_special_characters_in_program_args() -> None:
    template = _TEMPLATE_PATH.read_text()
    rendered = render_template(
        template,
        label="com.example.swarm-optimizer",
        program_args=["/bin/echo", '<tricky> & "quoted"'],
        working_dir="/tmp",
        hour1=0,
        minute1=0,
        hour2=12,
        minute2=0,
    )
    assert "&lt;tricky&gt;" in rendered
    assert "&amp;" in rendered


def test_render_never_invokes_a_subprocess() -> None:
    """Sanity check that this module is render-only (the "render-NOT-load"
    contract): it has no way to shell out to `launchctl` (or anything else)
    at all -- no `subprocess`/`os.system`/`os.exec*` import anywhere."""
    source = Path("scripts/swarm/launchd.py").read_text()
    assert "import subprocess" not in source
    assert "os.system" not in source
    assert "os.exec" not in source
