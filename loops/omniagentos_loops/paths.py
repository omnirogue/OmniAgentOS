"""Filesystem layout for the loop runtime.

Everything the runtime owns lives under ONE disposable root (``var/loops/``):
the venv, the per-family checkpoint databases, and nothing else. Rollback is
``rm -rf var/loops`` — the control-plane SQLite database is never touched by
this package's own storage (different write patterns, different blast radius;
MIGRATION_ARCHITECTURE.md "Persistence").
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from omniagentos_loops import REPO_ROOT

#: Loop family / instance / template names are used to build FILENAMES and to
#: build subprocess argv. A permissive name is a path-traversal and an argument
#: injection at once, so the vocabulary is deliberately tiny and anchored.
SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class UnsafeNameError(ValueError):
    """Raised when a family/instance/template name is not a safe identifier."""


def require_safe_name(value: str, *, kind: str) -> str:
    """Return *value* unchanged, or raise if it is not a safe identifier."""
    if not isinstance(value, str) or not SAFE_NAME_RE.match(value):
        raise UnsafeNameError(f"unsafe {kind} name {value!r}: expected {SAFE_NAME_RE.pattern}")
    return value


def loops_root() -> Path:
    """Root for all loop-runtime state (override: ``OMNIAGENTOS_LOOPS_ROOT``)."""
    override = os.environ.get("OMNIAGENTOS_LOOPS_ROOT")
    if override:
        return Path(override)
    return REPO_ROOT / "var" / "loops"


def venv_python() -> Path:
    """Interpreter of the loop-runtime venv (never the production venv).

    ``OMNIAGENTOS_LOOPS_VENV`` overrides it independently of
    ``OMNIAGENTOS_LOOPS_ROOT`` so a worktree (whose ``var/`` is empty) can run
    against the durable venv while writing its checkpoints somewhere disposable.
    """
    override = os.environ.get("OMNIAGENTOS_LOOPS_VENV")
    if override:
        return Path(override) / "bin" / "python"
    return loops_root() / "venv" / "bin" / "python"


def checkpoint_path(family: str) -> Path:
    """SqliteSaver database for one loop *family*.

    One file per family rather than one per instance: instances of the same
    family share a schema and a write cadence, and a family is the unit an
    operator would delete when rolling a loop back.
    """
    require_safe_name(family, kind="family")
    root = loops_root()
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{family}.ckpt.sqlite3"


__all__ = [
    "SAFE_NAME_RE",
    "UnsafeNameError",
    "checkpoint_path",
    "loops_root",
    "require_safe_name",
    "venv_python",
]
