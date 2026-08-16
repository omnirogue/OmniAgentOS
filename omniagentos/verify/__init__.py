"""
Mechanical syntax gate module.

This module provides a mechanical gate to verify Python syntax using ``py_compile``.
It defines a tri-state mode (off, shadow, enforce) configured via ``OMNIAGENTOS_VERIFY_GATE``.

To prevent "vacuous passes" where a check passes simply because there was nothing
to check (e.g. no valid files, or pytest collecting zero tests), these functions
fail closed when inputs are empty or tests are missing.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Literal

LOG = logging.getLogger(__name__)

VerifyMode = Literal["off", "shadow", "enforce"]

VERIFY_GATE_ENV = "OMNIAGENTOS_VERIFY_GATE"


def verify_mode(env: Mapping[str, str] | None = None) -> VerifyMode:
    """
    Parse the OMNIAGENTOS_VERIFY_GATE environment variable to determine the mechanical
    syntax gate mode. It must exactly match 'off', 'shadow', or 'enforce' (case-insensitive,
    stripped of whitespace). Any other value results in 'off'.
    """
    if env is None:
        env = os.environ
    val = env.get(VERIFY_GATE_ENV, "").strip().lower()
    if val == "shadow":
        return "shadow"
    if val == "enforce":
        return "enforce"
    return "off"


def verify_syntax(paths: Sequence[str], *, working_dir: str | None = None) -> tuple[bool, str]:
    """
    Run a python syntax check on the given paths. Fail-closed on a vacuous pass:
    if no valid python files remain to be checked, it returns False.
    """
    if working_dir is None:
        working_dir = "."

    py_files = []
    for p in paths:
        if not p.endswith(".py"):
            continue
        full_path = os.path.join(working_dir, p)
        if os.path.isfile(full_path):
            py_files.append(p)

    if not py_files:
        return False, "refusing vacuous pass: no valid Python files to check"

    # Run python -m py_compile ...
    cmd = [sys.executable, "-m", "py_compile", *py_files]
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    try:
        proc = subprocess.run(
            cmd,
            cwd=working_dir,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, "subprocess timeout expired during syntax check"
    except OSError as e:
        return False, f"OSError during syntax check: {e}"

    if proc.returncode == 0:
        return True, "syntax check passed"

    # Non-zero -> fail
    detail = proc.stderr
    if len(detail) > 4000:
        detail = detail[-4000:]
    return False, detail


def run_scoped_pytest(
    targets: Sequence[str], *, working_dir: str, timeout_s: int = 300
) -> tuple[bool, str]:
    """
    Run pytest against specific targets. Fail-closed on a vacuous pass if targets are empty
    or if pytest returns exit code 5 (no tests collected).
    """
    if not targets:
        return False, "refusing vacuous pass: no pytest targets"

    cmd = [sys.executable, "-m", "pytest", "-q", *targets]
    try:
        proc = subprocess.run(
            cmd,
            cwd=working_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, "subprocess timeout expired during pytest"
    except OSError as e:
        return False, f"OSError during pytest: {e}"

    if proc.returncode == 0:
        return True, "pytest passed"
    if proc.returncode == 5:
        return False, "pytest exit 5: no tests were collected for the derived targets"

    detail = proc.stderr if proc.stderr and proc.stderr.strip() else proc.stdout
    if len(detail) > 4000:
        detail = detail[-4000:]
    return False, f"pytest failed with exit code {proc.returncode}:\n{detail}"


def changed_python_files(working_dir: str) -> list[str]:
    """
    Discover changed .py files using git.
    Returns repo-relative paths ending in .py, deduped and sorted.
    """
    files = set()
    try:
        # Guard HEAD case
        proc_head = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "HEAD"],
            cwd=working_dir,
            capture_output=True,
            check=False,
        )
        if proc_head.returncode == 0:
            cmd_tracked = ["git", "diff", "--name-only", "HEAD"]
        else:
            cmd_tracked = ["git", "diff", "--name-only", "--cached"]

        # Tracked modifications
        proc_tracked = subprocess.run(
            cmd_tracked,
            cwd=working_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc_tracked.returncode == 0:
            for line in proc_tracked.stdout.splitlines():
                if line.endswith(".py"):
                    files.add(line)
        else:
            LOG.debug("changed_python_files git diff failed: %s", proc_tracked.stderr)

        # Untracked files
        proc_untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=working_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc_untracked.returncode == 0:
            for line in proc_untracked.stdout.splitlines():
                if line.endswith(".py"):
                    files.add(line)
        else:
            LOG.debug("changed_python_files git ls-files failed: %s", proc_untracked.stderr)

    except OSError as e:
        LOG.debug("changed_python_files git execution failed: %s", e)
        return []

    if proc_tracked.returncode != 0 and proc_untracked.returncode != 0:
        LOG.debug("changed_python_files all git commands failed")
        return []

    return sorted(list(files))


def verify_working_dir(working_dir: str) -> tuple[bool, str] | None:
    """
    Run verify_syntax on changed python files.
    Returns None if no .py files changed (not applicable).
    """
    changed = changed_python_files(working_dir)
    if not changed:
        return None

    return verify_syntax(changed, working_dir=working_dir)
