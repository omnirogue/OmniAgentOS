"""The install-*.sh shims must be able to import the launchd renderer.

Commit aa999b79 changed `scripts/scheduler/launchd.py` to a relative import
(`from ..lib.plist_render import render`). Every `scripts/scheduler/install-*.sh`
loads that file as a TOP-LEVEL module named `launchd` (it puts
`scripts/scheduler/` on `sys.path`), and a relative import has no parent package
to resolve against in that context — so all eight installers raised
ImportError and silently installed nothing. `com.omniagentos.banking` and
`com.omniagentos.revenue` were never created on this machine as a result.

These tests pin BOTH import shapes, because the module has two real callers:
the package path used by tests/tooling, and the top-level path used by the
shims that actually install the jobs.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEDULER_DIR = REPO_ROOT / "scripts" / "scheduler"


def test_installer_shim_import_shape_resolves() -> None:
    """Import it the way install-*.sh does: top-level, from scripts/scheduler/.

    Deliberately uses the MINIMAL sys.path shape — scripts/scheduler/ only,
    without the repo root. Five of the eight shims (cache-gc, metrics,
    modelintel, routines, install.sh) never put the repo root on sys.path, so a
    fallback that reached for `scripts.lib...` would still fail for them. This
    is the weakest caller, and it is the real contract.
    """
    # Run in a subprocess so the shim's sys.path shape cannot leak into or be
    # satisfied by this test session's already-imported modules, and set cwd
    # away from the repo so an implicit '' entry cannot rescue the import.
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(SCHEDULER_DIR)!r});"
        "from launchd import render_template;"
        "print('ok')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=tempfile.gettempdir(),
    )
    assert proc.returncode == 0, (
        "install-*.sh cannot import the launchd renderer — the installers are "
        f"broken and install nothing.\nstderr:\n{proc.stderr}"
    )
    assert "ok" in proc.stdout


def test_package_import_shape_still_resolves() -> None:
    """The normal package path must keep working for tooling and tests."""
    from scripts.scheduler.launchd import render_template

    assert callable(render_template)


def test_every_installer_that_imports_the_renderer_is_covered() -> None:
    """Guard the blast radius: name every shim this contract protects.

    All eight import `launchd` top-level, but they do NOT share a sys.path
    shape — three add the repo root, five do not. The test above pins the
    weakest shape so a fix cannot pass for the strong callers while leaving the
    weak ones broken, which is exactly how aa999b79 shipped.
    """
    shims = sorted(SCHEDULER_DIR.glob("install*.sh"))
    assert shims, "expected install-*.sh shims under scripts/scheduler/"

    importers = [s.name for s in shims if "from launchd import" in s.read_text(encoding="utf-8")]
    assert importers, "no installer imports the renderer — did the shims change shape?"
    assert len(importers) >= 8, (
        "expected at least the 8 known installer shims to import the renderer; "
        f"found {len(importers)}: {importers}"
    )
