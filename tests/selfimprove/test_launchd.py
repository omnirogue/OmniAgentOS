"""scripts/selfimprove/launchd.py — render-not-load launchd template for the
selfimprove curator (mirrors tests/lab/curator/test_launchd.py's shape)."""

from __future__ import annotations

from pathlib import Path

from scripts.selfimprove.launchd import render_template

_TEMPLATE_PATH = Path("scripts/selfimprove/com.omniagentos.selfimprove-curator.plist.template")


def test_template_fills_selfimprove_curator_launchd_values_for_two_daily_runs() -> None:
    template = _TEMPLATE_PATH.read_text()
    rendered = render_template(
        template,
        label="com.example.selfimprove-curator",
        program_args=["/usr/bin/python3", "-m", "omniagentos.selfimprove.curator"],
        working_dir="/tmp/omni agent",
        hour1=6,
        minute1=30,
        hour2=18,
        minute2=30,
    )
    assert "{{" not in rendered
    assert "<string>com.example.selfimprove-curator</string>" in rendered
    assert "<string>/usr/bin/python3</string>" in rendered
    assert "<string>-m</string>" in rendered
    assert "<string>omniagentos.selfimprove.curator</string>" in rendered
    assert "<string>/tmp/omni agent</string>" in rendered
    assert "<integer>6</integer>" in rendered
    assert "<integer>30</integer>" in rendered
    assert "<integer>18</integer>" in rendered
    # two StartCalendarInterval entries -> 2x/day
    assert rendered.count("<key>Hour</key>") == 2
    assert rendered.count("<key>Minute</key>") == 2


def test_rendered_plist_escapes_special_characters_in_program_args() -> None:
    template = _TEMPLATE_PATH.read_text()
    rendered = render_template(
        template,
        label="com.example.selfimprove-curator",
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
    source = Path("scripts/selfimprove/launchd.py").read_text()
    assert "import subprocess" not in source
    assert "os.system" not in source
    assert "os.exec" not in source
