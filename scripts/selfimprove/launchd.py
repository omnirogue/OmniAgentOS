"""Small, dependency-free renderer for the selfimprove-curator launchd plist.

Mirrors `scripts/curator/launchd.py`'s shape (L06 lab curator) but is owned
independently here: `scripts/curator/**` belongs to a different package, and
this helper is small enough that duplicating it is safer than importing
across sibling `scripts/*` script trees that are not real installed Python
packages.

Render-only, deliberately: nothing in this repo calls `launchctl load` for
this template. An operator who wants `python -m omniagentos.selfimprove.curator`
on a schedule renders it via `render_template` below and installs/loads the
result themselves — that install/load step is intentionally out of scope
here (see the package brief: "a rendered-NOT-loaded launchd template").
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
    """Fill the selfimprove-curator launchd template's two
    `StartCalendarInterval` entries (2x/day: HOUR1:MINUTE1 and HOUR2:MINUTE2)
    without invoking launchctl."""
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
