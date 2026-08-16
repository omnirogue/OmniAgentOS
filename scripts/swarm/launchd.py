"""Small, dependency-free renderer for the swarm-optimizer launchd plist.

Mirrors `scripts/selfimprove/launchd.py`'s shape (the selfimprove-curator
launchd idiom this package follows per plan doc WP8) but is owned
independently here: `scripts/selfimprove/**` belongs to a different package,
and this helper is small enough that duplicating it is safer than importing
across sibling `scripts/*` script trees that are not real installed Python
packages.

Render-only, deliberately: nothing in this repo calls `launchctl load` for
this template from Python. An operator who wants
`python -m omniagentos.swarm.optimize` on a schedule renders it via
`render_template` below (see `install-swarm-optimizer.sh`, which does the
actual write + `launchctl load`) — installing/loading is a deploy-time
decision, not something this renderer does on its own.
"""

from __future__ import annotations

from html import escape

try:  # imported as a package member: `scripts.swarm.launchd`
    from ..lib.plist_render import render
except ImportError:
    # install-swarm-optimizer.sh puts scripts/swarm/ on sys.path and imports
    # this as a top-level `launchd` module, which has no parent package for a
    # relative import to resolve against. The shim does not put the repo root
    # on sys.path, so the fallback must not depend on the caller's sys.path at
    # all — derive scripts/ from this file's own location.
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    from lib.plist_render import render


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
    """Fill the swarm-optimizer launchd template's two
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
