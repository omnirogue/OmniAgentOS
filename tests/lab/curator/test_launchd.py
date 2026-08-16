from __future__ import annotations

from pathlib import Path

from scripts.curator.launchd import render_template


def test_template_fills_curator_launchd_values_for_two_daily_runs() -> None:
    template = Path("scripts/curator/com.omniagentos.curator.plist.template").read_text()
    rendered = render_template(
        template,
        label="com.example.curator",
        program_args=["/usr/bin/python3", "-m", "omniagentos.lab.curator"],
        working_dir="/tmp/omni agent",
        hour1=7,
        minute1=15,
        hour2=19,
        minute2=45,
    )
    assert "{{" not in rendered
    assert "<string>com.example.curator</string>" in rendered
    assert "<string>/usr/bin/python3</string>" in rendered
    assert "<string>-m</string>" in rendered
    assert "<string>omniagentos.lab.curator</string>" in rendered
    assert "<string>/tmp/omni agent</string>" in rendered
    assert "<integer>7</integer>" in rendered
    assert "<integer>15</integer>" in rendered
    assert "<integer>19</integer>" in rendered
    assert "<integer>45</integer>" in rendered
    # two StartCalendarInterval entries -> 2x/day
    assert rendered.count("<key>Hour</key>") == 2
    assert rendered.count("<key>Minute</key>") == 2


def test_rendered_plist_escapes_special_characters_in_program_args() -> None:
    template = Path("scripts/curator/com.omniagentos.curator.plist.template").read_text()
    rendered = render_template(
        template,
        label="com.example.curator",
        program_args=["/bin/echo", '<tricky> & "quoted"'],
        working_dir="/tmp",
        hour1=0,
        minute1=0,
        hour2=12,
        minute2=0,
    )
    assert "&lt;tricky&gt;" in rendered
    assert "&amp;" in rendered
