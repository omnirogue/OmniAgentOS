"""Every install-*.sh shim must be able to import its launchd renderer.

Commit be023969 changed `scripts/scheduler/launchd.py` to a relative import
(`from ..lib.plist_render import render`). Every `scripts/scheduler/install-*.sh`
loads that file as a TOP-LEVEL module named `launchd`, and a relative import has
no parent package to resolve against in that context — so all eight installers
raised ImportError and silently installed nothing.

`tests/scripts/test_scheduler_installer_import.py` pinned that fix, but scoped
it to `scripts/scheduler/` only. The identical break was still live in four more
directories (issue #90): the agent watchdog, the planner canary, the health
sentinel, the blocked-session detector, the fable curator and the swarm
optimizer — six jobs that could not be installed at all.

This module is the class sweep, and it DISCOVERS its own subjects. Hardcoding
the list is how this defect keeps recurring: a new `scripts/<dir>/launchd.py`
copies the idiom, no test looks at it, and the installer is dead on arrival with
no signal. Writing this test against a discovered set immediately turned up four
directories that neither issue #90 nor its reporter had enumerated
(`archi-morning`, `backlog-executor`, `golden-suite`, `provider-sentinel`) —
all four already carried the fallback, but nothing had been checking.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

def _shim_directories() -> dict[str, list[Path]]:
    """Find every scripts/<dir>/ whose install shims import `launchd` top-level.

    This is the discovery step: it reads the shims themselves, so a new
    directory that copies the idiom is picked up without anyone remembering to
    add it to a list.
    """
    found: dict[str, list[Path]] = {}
    for shim in sorted(SCRIPTS_DIR.glob("*/install*.sh")):
        if "from launchd import" not in shim.read_text(encoding="utf-8"):
            continue
        if not (shim.parent / "launchd.py").is_file():
            continue
        found.setdefault(shim.parent.name, []).append(shim)
    return found


def _covered_directories() -> list[str]:
    return sorted(_shim_directories())


@pytest.mark.parametrize("dirname", _covered_directories())
def test_installer_shim_import_shape_resolves(dirname: str) -> None:
    """Import the renderer exactly as the shims do, or the installer installs nothing.

    Deliberately the WEAKEST caller shape — `scripts/<dir>/` only, no repo root.
    None of these shims put the repo root on sys.path, so a fallback reaching
    for `scripts.lib...` would still fail for them. Runs in a subprocess with
    cwd outside the repo so neither this session's already-imported modules nor
    an implicit '' path entry can rescue the import.
    """
    directory = SCRIPTS_DIR / dirname
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(directory)!r});"
        "from launchd import render_template;"
        "print('ok')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=tempfile.gettempdir(),
    )
    shims = ", ".join(s.name for s in _shim_directories()[dirname])
    assert proc.returncode == 0, (
        f"scripts/{dirname}/ ({shims}) cannot import the launchd renderer — "
        f"these installers are broken and render no plist.\nstderr:\n{proc.stderr}"
    )
    assert "ok" in proc.stdout


@pytest.mark.parametrize("dirname", _covered_directories())
def test_package_import_shape_still_resolves(dirname: str) -> None:
    """The package path must keep working for tooling and tests.

    The fallback is additive — the relative import is tried first and unchanged —
    so both callers of these modules have to keep resolving. Uses
    ``import_module`` with the literal directory name: several of these trees are
    hyphenated, which ``import x.y`` cannot spell but the import system resolves.
    """
    module = importlib.import_module(f"scripts.{dirname}.launchd")
    assert callable(module.render_template)


def test_discovery_finds_the_known_shim_directories() -> None:
    """The discovery itself must not silently match nothing.

    A glob that stops matching would make every test above vacuous — the exact
    failure mode this repo has been bitten by. Pins the floor at the set known
    when this was written, so the contract can only widen.
    """
    discovered = set(_shim_directories())
    known = {
        "archi-morning",
        "backlog-executor",
        "fable-curator",
        "gates",
        "golden-suite",
        "health-sentinel",
        "provider-sentinel",
        "scheduler",
        "swarm",
    }
    missing = known - discovered
    assert not missing, (
        f"discovery stopped finding shim directories {sorted(missing)} — the "
        "glob or the shims changed shape, and these tests just went vacuous."
    )
