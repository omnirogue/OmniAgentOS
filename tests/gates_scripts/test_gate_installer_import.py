"""The gate install-*.sh shims must be able to import the launchd renderer.

`scripts/gates/launchd.py` imported its renderer with `from ..lib.plist_render
import render`. Both `scripts/gates/install-*.sh` shims load that file as a
TOP-LEVEL module named `launchd` (they put only `scripts/gates/` on
`sys.path`), and a relative import has no parent package to resolve against in
that context -- so both installers raised ImportError, exited 1 and rendered
no plist. `com.omniagentos.agent-watchdog` and
`com.omniagentos.planner-canary` could not be created at all.

This is the same failure `tests/scripts/test_scheduler_installer_import.py`
pins for `scripts/scheduler/`; see issue #90 for the four directories that
carry it. These tests pin BOTH import shapes, because the module has two real
callers: the package path used by tests and tooling, and the top-level path
used by the shims that actually install the jobs.

The end-to-end test deliberately does NOT stub python3 on PATH.
`test_gate_installers.py` substitutes a fake interpreter that writes
'rendered' directly, which is right for what it asserts (label traversal) but
means it never executes the renderer -- 13 such tests passed while both
installers were dead.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATES_DIR = REPO_ROOT / "scripts" / "gates"


def test_installer_shim_import_shape_resolves() -> None:
    """Import it the way install-agent-watchdog.sh does: top-level, gates/ only.

    Uses the MINIMAL sys.path shape -- scripts/gates/ without the repo root --
    because neither shim puts the repo root on sys.path, so a fallback that
    reached for `scripts.lib...` would still fail for both. This is the real
    contract.
    """
    # Subprocess so the shim's sys.path shape cannot be satisfied by modules
    # this test session already imported, and cwd away from the repo so an
    # implicit '' entry cannot rescue the import.
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(GATES_DIR)!r});"
        "from launchd import render_template;"
        "print('ok')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=tempfile.gettempdir(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_package_import_shape_still_resolves() -> None:
    """The package path used by tests and tooling must keep working."""
    code = "from scripts.gates.launchd import render_template; print('ok')"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_agent_watchdog_installer_actually_renders_a_plist(tmp_path: Path) -> None:
    """End to end with the REAL python3: the installer must write its plist."""
    render_dir = tmp_path / "rendered"
    env = os.environ.copy()
    env.update(
        {
            "OMNIAGENTOS_VAR_DIR": str(tmp_path / "var"),
            "OMNIAGENTOS_LAUNCHD_TARGET_DIR": str(render_dir),
            "HOME": str(tmp_path / "home"),
        }
    )
    (tmp_path / "home").mkdir()

    result = subprocess.run(
        ["sh", str(GATES_DIR / "install-agent-watchdog.sh")],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert "attempted relative import" not in result.stderr, result.stderr
    plist = render_dir / "com.omniagentos.agent-watchdog.plist"
    assert plist.exists(), f"installer rendered nothing: {result.stdout}{result.stderr}"
    assert "com.omniagentos.agent-watchdog" in plist.read_text(encoding="utf-8")
