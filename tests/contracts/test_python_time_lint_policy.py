"""Contract test for the TID251 time.mktime ban in pyproject.toml.

WHY THIS EXISTS
----------------
time.mktime interprets its struct_time argument as LOCAL wall time. Every
timestamp this codebase carries is UTC (Z-suffixed), so a mktime-based epoch
conversion silently misreads UTC values under any non-UTC host TZ. Two live
carriers made exactly this mistake (pipeline/bridge/file_proposal.py's expiry
check and pipeline/bridge/governor.py's probe-age check, both fixed alongside
this policy). A prose warning or a fix to the two known call sites does not
prevent a THIRD sibling call site from reintroducing the same bug -- only a
mechanical, always-on check does.

This test does not reimplement Ruff's AST logic. It runs the repository's
OWN pinned `ruff` binary, through the repository's OWN `pyproject.toml`
configuration, against small synthetic snippets, and asserts on Ruff's own
verdict (TID251 present or absent). That is the policy under test: the
CONFIGURED lint surface, not a private reimplementation of it.

Covers, per the falsifier:
  * a direct `time.mktime(...)` call            -> banned (TID251)
  * an imported-alias `from time import mktime`  -> banned (TID251)
  * a `calendar.timegm(...)` negative control     -> stays green
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ruff_binary() -> str:
    """The pinned ruff this repo already runs everywhere else.

    Prefers the repo's own venv (matches what CI/gates invoke) and falls back
    to whatever `ruff` resolves on PATH so this test does not depend on the
    caller's specific venv layout.
    """
    venv_ruff = Path(sys.executable).resolve().parent / "ruff"
    if venv_ruff.exists():
        return str(venv_ruff)
    found = shutil.which("ruff")
    if found:
        return found
    pytest.skip("no ruff binary available on this host")


def _ruff_check(tmp_path: Path, source: str) -> subprocess.CompletedProcess:
    """Write `source` to a throwaway .py file inside `tmp_path` and run the
    repo's configured `ruff check` on it. cwd=REPO_ROOT so Ruff discovers
    THIS repo's pyproject.toml (including the TID251 banned-api entry),
    exactly as every other canonical invocation does."""
    target = tmp_path / "synthetic_snippet.py"
    target.write_text(source)
    return subprocess.run(
        [_ruff_binary(), "check", "--select", "TID", "--no-cache", str(target)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_direct_time_mktime_call_is_banned(tmp_path):
    result = _ruff_check(tmp_path, "import time\ntime.mktime(time.gmtime())\n")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "TID251" in result.stdout, result.stdout + result.stderr
    assert "time.mktime" in result.stdout, result.stdout + result.stderr


def test_imported_alias_mktime_call_is_banned(tmp_path):
    result = _ruff_check(
        tmp_path, "from time import mktime, gmtime\nmktime(gmtime())\n"
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "TID251" in result.stdout, result.stdout + result.stderr


def test_calendar_timegm_negative_control_stays_green(tmp_path):
    result = _ruff_check(
        tmp_path, "import calendar, time\ncalendar.timegm(time.gmtime())\n"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TID251" not in result.stdout, result.stdout + result.stderr
