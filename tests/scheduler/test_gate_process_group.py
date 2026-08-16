"""Tests for B1 process-group containment and descendant termination."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from omniagentos.scheduler.gate_evidence import GateEvidenceStore
from omniagentos.scheduler.gate_runner import GateRunRequest, PytestGateRunner


def _git_workspace(tmp_path: Path, code: str) -> tuple[Path, Path]:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    (root / "suite").mkdir(parents=True, exist_ok=True)
    pid_file = tmp_path / "grandchild.pid"
    formatted_code = code.replace("{pid_file}", str(pid_file))
    (root / "suite" / "test_spawn.py").write_text(formatted_code, encoding="utf-8")
    (root / ".gitignore").write_text(".pytest_cache\n__pycache__\nvar\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(root), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(root),
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(root), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(root), check=True, capture_output=True)
    return root, pid_file


def test_timeout_kills_detached_grandchild_process_group(tmp_path: Path) -> None:
    """P1 / B1 probe: a test spawning a grandchild holding stdout sleeping 60s must be killed."""
    test_code = """
import subprocess
import sys
import time

def test_spawn_grandchild() -> None:
    pid_path = "{pid_file}"
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import os, time; open('{pid_path}', 'w').write(str(os.getpid())); time.sleep(60)"],
    )
    time.sleep(60)
"""
    workspace, pid_file = _git_workspace(tmp_path, test_code)
    store = GateEvidenceStore(tmp_path / "gate-evidence")
    runner = PytestGateRunner(store, timeout_seconds=1)

    req = GateRunRequest(
        routine_id="rt-1",
        run_id="run-1",
        iteration=1,
        gate_type="test_command",
        gate_config={"command": "pytest suite", "expected_exit_code": 0},
        workspace=workspace,
    )

    t0 = time.monotonic()
    evidence = runner.run(req)
    t1 = time.monotonic()

    assert t1 - t0 < 15.0
    assert evidence.exit_code == 124

    assert pid_file.exists(), "grandchild never wrote pid file"
    grandchild_pid = int(pid_file.read_text().strip())

    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)
