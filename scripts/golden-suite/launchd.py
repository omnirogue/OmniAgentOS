"""Small, dependency-free renderer for the golden-suite launchd plist.

Mirrors `scripts/swarm/launchd.py`'s shape (the launchd idiom this package
follows per the plan doc's A0.0 golden-suite bullet) but is owned
independently here: `scripts/swarm/**` belongs to a different package, and
this helper is small enough that duplicating it is safer than importing
across sibling `scripts/*` script trees that are not real installed Python
packages (and `scripts/golden-suite` -- a hyphenated directory name --
can never be a dotted `scripts.golden_suite.launchd` import target anyway).

Render-only, deliberately: nothing in this repo calls `launchctl load` for
this template from Python. An operator who wants
`scripts/golden-suite/golden-suite.sh` on a schedule renders it via
`render_template` below (see `install-golden-suite.sh`, which does the
actual write + `launchctl load`) -- installing/loading is a deploy-time
decision, not something this renderer does on its own.
"""

from __future__ import annotations

from html import escape

try:  # imported as a real package member, e.g. `scripts.archi_morning.launchd`
    from ..lib.plist_render import render
except ImportError:
    # The install-*.sh shim puts this file's own directory on sys.path and
    # imports it as a top-level `launchd` module, which has no parent package
    # for a relative import to resolve against (mirrors the same fallback in
    # scripts/scheduler/launchd.py). Derive scripts/ from this file's own
    # location so the fallback doesn't depend on the caller's sys.path.
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
    hour: int,
    minute: int,
) -> str:
    """Fill the golden-suite launchd template's single daily
    `StartCalendarInterval` entry (HOUR:MINUTE, 01:00 by default) without
    invoking launchctl."""
    replacements = {
        "{{LABEL}}": escape(label),
        "{{PROGRAM_ARGS}}": _args_xml(program_args),
        "{{WORKING_DIR}}": escape(working_dir),
        "{{HOUR}}": str(hour),
        "{{MINUTE}}": str(minute),
    }
    return render(template, replacements)
