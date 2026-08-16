"""Regression test: _changed_py must survive spaces and non-ASCII bytes.

scripts/reachability-gate.py:128 used to walk the diff with `git diff --name-only`
and split the result on Python whitespace (`out.split()`). Under git's default
`core.quotePath=true`, a non-ASCII path is emitted C-quoted (leading `"`), and a
space anywhere in a path breaks it into two tokens neither of which survives the
`.py` / `omniagentos/` filter. Both carriers make `_changed_py` silently drop the
file, so the reachability gate never opens it and reports a favourable absence.

This test builds a throwaway git repo, copies the real gate script into it (so the
gate's own `REPO = Path(__file__).resolve().parent.parent` resolves inside the
throwaway repo, not the real checkout), commits an ordinary path plus a
space-bearing path plus a non-ASCII path, and asserts `_changed_py` returns all
three, using a NUL-delimited walk that cannot be defeated by either carrier.

`core.quotePath` is pinned explicitly to `true` in every throwaway repo this file
creates. Without that pin, the non-ASCII red-first proof is environment-dependent:
on a machine whose global git config sets `core.quotePath=false`, git would emit
the café.py path unquoted, the OLD `.split()` code would handle it fine, and the
regression test would pass against the buggy implementation. Only the space carrier
was ever unconditional; the non-ASCII carrier needs the pin to be deterministic.

A second test below covers the same defect class in `_mounted_routers`, which used
`git grep -l` (also C-quoted under core.quotePath=true) without `-z`.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/reachability-gate.py"

# core.quotePath is pinned true (git's own default) so the non-ASCII red-first
# proof is deterministic regardless of the machine's global git config.
_IDENTITY = (
    "-c",
    "user.name=test",
    "-c",
    "user.email=test@example.com",
    "-c",
    "core.quotePath=true",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *_IDENTITY, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _candidate_repo(tmp_path: Path) -> Path:
    """A checkout holding the real gate, with a base commit and a candidate
    branch that changes an ordinary path, a space-bearing path, and a
    non-ASCII path.
    """
    repo = tmp_path / "checkout"
    gate = repo / "scripts/reachability-gate.py"
    gate.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, gate)

    _git(repo, "init", "-q", "-b", "main")
    # Persist the pin into the repo's own config, not just this invocation's
    # `-c` flag: the gate under test calls plain `git diff`/`git grep` (no `-c`)
    # via its own `_run`, which reads the repo's on-disk config. Without this,
    # the test would be deterministic only for the commands *this file* issues,
    # not for the ones the gate itself issues.
    _git(repo, "config", "core.quotePath", "true")
    (repo / "omniagentos").mkdir()
    (repo / "omniagentos" / "keep.py").write_text("x = 1\n", encoding="utf-8")
    _commit(repo, "base")

    _git(repo, "checkout", "-q", "-b", "candidate")
    (repo / "omniagentos" / "ordinary.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "omniagentos" / "my notes.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "omniagentos" / "café.py").write_text("x = 1\n", encoding="utf-8")
    _commit(repo, "add three changed files")
    return repo


def _load_gate_module(repo: Path) -> ModuleType:
    """Import the copied gate script as a fresh module so its module-level
    REPO constant resolves relative to the throwaway repo, not the real one.
    """
    gate_path = repo / "scripts/reachability-gate.py"
    spec = importlib.util.spec_from_file_location(
        "reachability_gate_under_test", gate_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _mounted_router_repo(tmp_path: Path) -> Path:
    """A checkout holding the real gate, with a served FastAPI app and a
    router mounted from a non-ASCII-named module (``omniagentos/café.py``).
    """
    repo = tmp_path / "checkout"
    gate = repo / "scripts/reachability-gate.py"
    gate.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, gate)

    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "core.quotePath", "true")
    (repo / "omniagentos").mkdir()
    (repo / "omniagentos" / "app.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    (repo / "omniagentos" / "café.py").write_text(
        "from fastapi import APIRouter\n"
        "from omniagentos.app import app\n"
        "router = APIRouter()\n"
        "app.include_router(router)\n",
        encoding="utf-8",
    )
    _commit(repo, "app with router mounted from a non-ascii-named module")
    return repo


def test_mounted_routers_survives_non_ascii_path(tmp_path: Path) -> None:
    """`_mounted_routers` used `git grep -l` (no `-z`), so a non-ASCII path is
    C-quoted under core.quotePath=true and the quoted literal then fails every
    downstream `git show {branch}:{f}` lookup — the router mounted from
    omniagentos/café.py is silently invisible, exactly the same favourable
    absence as _changed_py before its fix.
    """
    repo = _mounted_router_repo(tmp_path)
    gate = _load_gate_module(repo)

    mounted = gate._mounted_routers("main")

    assert "omniagentos.café:router" in mounted


def test_changed_py_survives_space_and_non_ascii(tmp_path: Path) -> None:
    repo = _candidate_repo(tmp_path)
    gate = _load_gate_module(repo)

    changed = gate._changed_py("main", "candidate")

    assert "omniagentos/ordinary.py" in changed
    assert "omniagentos/my notes.py" in changed
    assert "omniagentos/café.py" in changed
    assert len(changed) == 3

    # Pin the two specific tokenisation carriers, not just the count: a future
    # change that returns the right NUMBER of wrong strings must still fail.
    assert "omniagentos/my" not in changed
    assert not any(f.startswith('"') for f in changed)
