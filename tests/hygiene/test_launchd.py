"""scripts/hygiene/launchd.py -- render-not-load launchd template for the
nightly hygiene sweep (mirrors tests/archi-morning/test_launchd.py's single
daily StartCalendarInterval shape, tests/swarm/test_launchd.py's twice-daily
one)."""

from __future__ import annotations

from pathlib import Path

from scripts.hygiene.launchd import render_template

_TEMPLATE_PATH = Path("scripts/hygiene/com.omniagentos.hygiene.plist.template")


def test_template_fills_hygiene_launchd_values_for_the_daily_run() -> None:
    template = _TEMPLATE_PATH.read_text()
    rendered = render_template(
        template,
        label="com.example.hygiene",
        program_args=["/bin/sh", "/tmp/hygiene.sh"],
        working_dir="/tmp/omni agent",
        hour=4,
        minute=15,
    )
    assert "{{" not in rendered
    assert "<string>com.example.hygiene</string>" in rendered
    assert "<string>/bin/sh</string>" in rendered
    assert "<string>/tmp/hygiene.sh</string>" in rendered
    assert "<string>/tmp/omni agent</string>" in rendered
    assert "<integer>4</integer>" in rendered
    assert "<integer>15</integer>" in rendered
    # single daily StartCalendarInterval -> exactly one Hour/Minute pair.
    assert rendered.count("<key>Hour</key>") == 1
    assert rendered.count("<key>Minute</key>") == 1
    assert "var/log/hygiene.log" in rendered


def test_rendered_plist_escapes_special_characters_in_program_args() -> None:
    template = _TEMPLATE_PATH.read_text()
    rendered = render_template(
        template,
        label="com.example.hygiene",
        program_args=["/bin/echo", '<tricky> & "quoted"'],
        working_dir="/tmp",
        hour=4,
        minute=15,
    )
    assert "&lt;tricky&gt;" in rendered
    assert "&amp;" in rendered


def test_render_never_invokes_a_subprocess() -> None:
    """Render-NOT-load contract: no way to shell out to `launchctl` (or
    anything else) at all -- no subprocess/os.system/os.exec* import."""
    source = Path("scripts/hygiene/launchd.py").read_text()
    assert "import subprocess" not in source
    assert "os.system" not in source
    assert "os.exec" not in source
