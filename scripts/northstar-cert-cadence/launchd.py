"""Small, dependency-free renderer for the nscert-t1 launchd plist.

Mirrors `scripts/golden-suite/launchd.py` (the daily `StartCalendarInterval`
shape) but is owned independently here, per the established idiom: sibling
`scripts/*` trees are not installed Python packages -- and a hyphenated
directory name can never be a dotted import target anyway -- so duplicating
this tiny renderer is safer than importing across them.

Render-only, deliberately: nothing here calls `launchctl`. `install.sh` does
the write, the lint, the copy into `~/Library/LaunchAgents` and the
bootout/bootstrap.
"""

from __future__ import annotations

from html import escape

try:  # imported as a package member
    from ..lib.plist_render import render
except ImportError:
    # install.sh puts scripts/northstar-cert-cadence/ on sys.path and imports
    # this as a top-level `launchd` module, which has no parent package for a
    # relative import to resolve against. Derive scripts/ from this file's own
    # location so the fallback does not depend on the caller's sys.path.
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
    """Fill the nscert-t1 template's single daily `StartCalendarInterval` entry."""
    replacements = {
        "{{LABEL}}": escape(label),
        "{{PROGRAM_ARGS}}": _args_xml(program_args),
        "{{WORKING_DIR}}": escape(working_dir),
        "{{HOUR}}": str(int(hour)),
        "{{MINUTE}}": str(int(minute)),
    }
    return render(template, replacements)
