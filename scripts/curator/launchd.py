"""Small, dependency-free renderer for the curator launchd plist (2x/day).

Mirrors `scripts/scheduler/launchd.py`'s shape (H1) but is owned
independently by L06: `scripts/scheduler/**` belongs to a different package,
and this helper is small enough that duplicating it here is safer than
importing across sibling `scripts/*` script trees that are not real
installed Python packages.
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
    hour1: int,
    minute1: int,
    hour2: int,
    minute2: int,
) -> str:
    """Fill the curator launchd template's two `StartCalendarInterval` entries
    (2x/day: HOUR1:MINUTE1 and HOUR2:MINUTE2) without invoking launchctl."""
    replacements = {
        "{{LABEL}}": escape(label),
        "{{PROGRAM_ARGS}}": _args_xml(program_args),
        "{{WORKING_DIR}}": escape(working_dir),
        "{{HOUR1}}": str(hour1),
        "{{MINUTE1}}": str(minute1),
        "{{HOUR2}}": str(hour2),
        "{{MINUTE2}}": str(minute2),
    }
    return render(template, replacements)


render_plist_template = render_template
