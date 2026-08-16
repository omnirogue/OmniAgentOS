from __future__ import annotations

import plistlib
import sys
from pathlib import Path


def test_steward_templates_render_with_launchd_helper() -> None:
    scheduler = Path(__file__).resolve().parents[2] / "scripts" / "scheduler"
    sys.path.insert(0, str(scheduler))
    try:
        from launchd import render_template  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    templates = sorted(scheduler.glob("com.omniagentos.steward.*.plist.template"))
    assert len(templates) == 4
    parsed = {}
    for template in templates:
        name = template.name.removeprefix("com.omniagentos.steward.").removesuffix(
            ".plist.template"
        )
        rendered = render_template(
            template.read_text(encoding="utf-8"),
            label=f"com.omniagentos.steward.{name}",
            program_args=["/python3.12", "-m", f"example.{name}"],
            working_dir="/repo & worktree",
            hour=7,
            minute=30,
        )
        assert "{{" not in rendered
        parsed[name] = plistlib.loads(rendered.encode("utf-8"))

    assert parsed["briefing"]["StartCalendarInterval"] == {"Hour": 7, "Minute": 30}
    assert parsed["metrics"]["StartInterval"] == 3600
    assert parsed["alerts"]["StartInterval"] == 900
    assert parsed["comms"]["StartCalendarInterval"] == {"Hour": 7, "Minute": 30}
