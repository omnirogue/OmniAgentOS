"""Small, dependency-free renderer for the hygiene launchd plist.

Mirrors `scripts/archi-morning/launchd.py`'s shape (itself mirroring
`scripts/swarm/launchd.py` / `scripts/selfimprove/launchd.py`) but is owned
independently here: sibling `scripts/*` trees are not real installed Python
packages, and this helper is small enough that duplicating it is safer than
importing across them — the established idiom for every launchd job in this
repo.

Render-only, deliberately: nothing here calls `launchctl load`. The single
daily `StartCalendarInterval` (one Hour/Minute dict, NOT the twice-daily
array-of-dicts shape) is filled by `render_template` below;
`install-hygiene.sh` does the actual write + lint + `launchctl load`.
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
    """Fill the hygiene launchd template's single daily
    `StartCalendarInterval` entry (HOUR:MINUTE, default 4:15) without
    invoking launchctl."""
    replacements = {
        "{{LABEL}}": escape(label),
        "{{PROGRAM_ARGS}}": _args_xml(program_args),
        "{{WORKING_DIR}}": escape(working_dir),
        "{{HOUR}}": str(hour),
        "{{MINUTE}}": str(minute),
    }
    return render(template, replacements)
