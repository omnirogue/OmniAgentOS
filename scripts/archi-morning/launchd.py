"""Small, dependency-free renderer for the archi-morning launchd plist.

Mirrors `scripts/swarm/launchd.py`'s shape (itself mirroring
`scripts/selfimprove/launchd.py`) but is owned independently here: sibling
`scripts/*` trees are not real installed Python packages, and this helper is
small enough that duplicating it is safer than importing across them — the
established idiom for every launchd job in this repo.

Render-only, deliberately: nothing here calls `launchctl load`. The single
daily `StartCalendarInterval` (one Hour/Minute dict, NOT the twice-daily
array-of-dicts shape) is filled by `render_template` below;
`install-archi-morning.sh` does the actual write + lint + `launchctl load`.
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
    """Fill the archi-morning launchd template's single daily
    `StartCalendarInterval` entry (HOUR:MINUTE) without invoking launchctl."""
    replacements = {
        "{{LABEL}}": escape(label),
        "{{PROGRAM_ARGS}}": _args_xml(program_args),
        "{{WORKING_DIR}}": escape(working_dir),
        "{{HOUR}}": str(hour),
        "{{MINUTE}}": str(minute),
    }
    return render(template, replacements)
