"""Small, dependency-free renderer for the fable-curator launchd plist (1x/day).

Mirrors `scripts/curator/launchd.py`'s shape but renders a single
`StartCalendarInterval` entry. Duplicated rather than imported across
sibling `scripts/*` trees, which are not real installed Python packages
(same reasoning as scripts/curator/launchd.py).
"""

from __future__ import annotations

from html import escape

try:  # imported as a package member: `scripts.fable-curator.launchd`
    from ..lib.plist_render import render
except ImportError:
    # install.sh puts scripts/fable-curator/ on sys.path and imports this as a
    # top-level `launchd` module, which has no parent package for a relative
    # import to resolve against. The shim does not put the repo root on
    # sys.path, so the fallback must not depend on the caller's sys.path at
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
    hour: int,
    minute: int,
) -> str:
    """Fill the fable-curator launchd template's single
    `StartCalendarInterval` entry (1x/day at HOUR:MINUTE) without invoking
    launchctl."""
    replacements = {
        "{{LABEL}}": escape(label),
        "{{PROGRAM_ARGS}}": _args_xml(program_args),
        "{{WORKING_DIR}}": escape(working_dir),
        "{{HOUR}}": str(int(hour)),
        "{{MINUTE}}": str(int(minute)),
    }
    return render(template, replacements)


render_plist_template = render_template
