from pathlib import Path

from scripts.scheduler.launchd import render_template


def test_template_fills_launchd_values():
    template = Path("scripts/scheduler/com.omniagentos.morning.plist.template").read_text()
    rendered = render_template(
        template,
        label="com.example.morning",
        program_args=["/usr/bin/python3", "-m", "omniagentos.scheduler.morning_report"],
        working_dir="/tmp/omni agent",
        hour=7,
        minute=30,
    )
    assert "{{" not in rendered
    assert "<string>com.example.morning</string>" in rendered
    assert "<string>/usr/bin/python3</string>" in rendered
    assert "<string>/tmp/omni agent</string>" in rendered
    assert "<integer>7</integer>" in rendered
    assert "<integer>30</integer>" in rendered


def test_routines_template_fills_values_and_ticks_every_5_minutes():
    template = Path("scripts/scheduler/com.omniagentos.routines.plist.template").read_text()
    rendered = render_template(
        template,
        label="com.omniagentos.routines",
        program_args=["/usr/bin/python3", "-m", "omniagentos.scheduler.routines_tick"],
        working_dir="/tmp/omni agent",
        hour=0,
        minute=0,
    )
    assert "{{" not in rendered
    assert "<string>com.omniagentos.routines</string>" in rendered
    assert "-m</string>" in rendered
    assert "<string>omniagentos.scheduler.routines_tick</string>" in rendered
    # StartInterval (every 5 minutes), NOT StartCalendarInterval: routines fire
    # on a cadence, not once a day.
    assert "<key>StartInterval</key><integer>300</integer>" in rendered
    assert "StartCalendarInterval" not in rendered
