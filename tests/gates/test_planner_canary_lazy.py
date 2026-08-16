"""Lazy path-resolution tests for the planner canary gate.

The merge gate's whole-repo ``pytest --collect-only`` executes import-time
code, so the canary module must not resolve or touch anything under var/
until a probe actually records a result.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omniagentos.gates import planner_canary

ROOT = Path(__file__).resolve().parents[2]

# Runs in a subprocess so the import is fresh: an audit hook records every
# open / os.mkdir under <repo>/var while the module imports.
_IMPORT_AUDIT = """\
import json
import sys

var_root = sys.argv[1]
hits = []


def _hook(event, args):
    if event not in {"open", "os.mkdir"}:
        return
    path = args[0]
    if path is None or isinstance(path, int):
        return
    if isinstance(path, bytes):
        path = path.decode(errors="replace")
    if str(path).startswith(var_root):
        hits.append([event, str(path)])


sys.addaudithook(_hook)
import omniagentos.gates.planner_canary as mod

print(json.dumps({"hits": hits, "has_log_path_const": hasattr(mod, "LOG_PATH")}))
"""


def test_import_performs_no_filesystem_access_under_var() -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_AUDIT, str(ROOT / "var")],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["hits"] == []
    # The eager module constant is gone; resolution lives in _log_path().
    assert payload["has_log_path_const"] is False


def test_default_resolution_matches_pre_change_path() -> None:
    assert planner_canary._log_path() == ROOT / "var" / "log" / "planner-canary.jsonl"


def test_var_env_knobs_do_not_move_the_log(monkeypatch: pytest.MonkeyPatch) -> None:
    # Deliberate: the deployed launchd job points OMNIAGENTOS_VAR(_DIR) at
    # var/runtime, so honoring them would move the log away from the
    # pre-change var/log/ location.
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(ROOT / "var" / "runtime"))
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(ROOT / "var" / "runtime"))
    assert planner_canary._log_path() == ROOT / "var" / "log" / "planner-canary.jsonl"
