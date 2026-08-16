"""Small dependency-free renderer for gate launchd templates."""

from __future__ import annotations

from html import escape

try:  # imported as a package member: `scripts.gates.launchd`
    from ..lib.plist_render import render
except ImportError:
    # install-agent-watchdog.sh and install-planner-canary.sh put scripts/gates/
    # on sys.path and import this as a top-level `launchd` module, which has no
    # parent package for a relative import to resolve against. Neither shim puts
    # the repo root on sys.path, so the fallback must not depend on the caller's
    # sys.path at all -- derive scripts/ from this file's own location.
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    from lib.plist_render import render


def render_template(
    template: str, *, label: str, program_args: list[str], working_dir: str, interval: int
) -> str:
    """Render a StartInterval plist without invoking launchctl."""
    if type(interval) is not int or interval <= 0:
        raise ValueError("interval must be a positive integer")

    args = "\n".join(f"        <string>{escape(arg)}</string>" for arg in program_args)
    values = {
        "{{LABEL}}": escape(label),
        "{{PROGRAM_ARGS}}": f"<array>\n{args}\n    </array>",
        "{{WORKING_DIR}}": escape(working_dir),
        "{{INTERVAL}}": str(interval),
    }
    return render(template, values)
