"""Small, dependency-free renderer for the reflection loop launchd plists.

Mirrors `scripts/archi-morning/launchd.py` but is owned independently here.
"""

from __future__ import annotations

from html import escape

from ..lib.plist_render import render


def _args_xml(program_args: list[str]) -> str:
    values = "\n".join(f"        <string>{escape(arg)}</string>" for arg in program_args)
    return "<array>\n" + values + "\n    </array>"


def render_template(
    template: str,
    *,
    label: str,
    program_args: list[str],
    working_dir: str,
    hour: int,
    minute: int,
) -> str:
    """Fill the launchd template's single daily StartCalendarInterval entry (HOUR:MINUTE) without invoking launchctl."""
    replacements = {
        "{{LABEL}}": escape(label),
        "{{PROGRAM_ARGS}}": _args_xml(program_args),
        "{{WORKING_DIR}}": escape(working_dir),
        "{{HOUR}}": str(hour),
        "{{MINUTE}}": str(minute),
    }
    return render(template, replacements)
