"""Small, dependency-free renderer for the health-sentinel launchd plist.

Mirrors ``scripts/gates/launchd.py``'s shape (a single ``StartInterval``) but is
owned independently here, per the established idiom: sibling ``scripts/*`` trees
are not installed Python packages, so duplicating this tiny renderer is safer
than importing across them.

Render-only, deliberately: nothing here calls ``launchctl``. ``install.sh`` does
the write, the lint, the copy into ``~/Library/LaunchAgents`` and the
bootout/bootstrap.
"""

from __future__ import annotations

from html import escape

try:  # imported as a package member: `scripts.health-sentinel.launchd`
    from ..lib.plist_render import render
except ImportError:
    # install.sh and install-blocked-session-detector.sh put scripts/health-sentinel/
    # on sys.path and import this as a top-level `launchd` module, which has no
    # parent package for a relative import to resolve against. Neither shim puts
    # the repo root on sys.path, so the fallback must not depend on the caller's
    # sys.path at all — derive scripts/ from this file's own location.
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    from lib.plist_render import render


def render_template(
    template: str,
    *,
    label: str,
    program_args: list[str],
    working_dir: str,
    interval: int,
) -> str:
    """Fill the health-sentinel template's single ``StartInterval`` entry."""
    args = "\n".join(f"        <string>{escape(arg)}</string>" for arg in program_args)
    values = {
        "{{LABEL}}": escape(label),
        "{{PROGRAM_ARGS}}": f"<array>\n{args}\n    </array>",
        "{{WORKING_DIR}}": escape(working_dir),
        "{{INTERVAL}}": str(int(interval)),
    }
    return render(template, values)
