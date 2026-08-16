"""``--slots N``: fork N workers, respawn what dies, drain on SIGTERM.

Run out of process, because the thing under test IS process behaviour: forking
inside the pytest process would fork the test session too. The parent runs no
queue logic at all — a supervisor that could itself block on a claim would be a
second failure mode for no benefit — so this is the whole of its contract.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

DRIVER = """
import os, sys, time
sys.path.insert(0, "__REPO__")
from omniagentos.workqueue.worker import supervise
import omniagentos.workqueue.worker as w
w.RESPAWN_BACKOFF_S = 0.2
marks = "__MARKS__"

def child():
    # One line per child START, so a respawn is visible as a second line.
    with open(marks, "a") as fh:
        fh.write(f"start {os.getpid()}\\n")
        fh.flush()
    if len(open(marks).read().splitlines()) <= 2:
        return 0          # the first two children exit immediately -> must respawn
    time.sleep(30)        # then stay up until SIGTERM drains us
    return 0

sys.exit(supervise(2, child))
"""


@pytest.mark.smoke
def test_slots_fork_respawn_and_drain(tmp_path: Path) -> None:
    marks = tmp_path / "starts.txt"
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER.replace("__REPO__", str(REPO_ROOT)).replace("__MARKS__", str(marks)))
    proc = subprocess.Popen([sys.executable, str(driver)], cwd=str(REPO_ROOT))
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if marks.exists() and len(marks.read_text().splitlines()) >= 4:
                break
            time.sleep(0.2)
        starts = marks.read_text().splitlines()
        assert len(starts) >= 4, f"a dead slot was not respawned: {starts}"
        assert len({line.split()[1] for line in starts}) == len(starts), "pids must be distinct"

        # Drain: the parent forwards SIGTERM and reaps. A real child replaces the
        # inherited disposition with the Worker's stop handler and finishes its
        # in-flight unit first; this driver has no Worker, so it takes SIG_DFL.
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a failed assertion above
            proc.kill()
            proc.wait(timeout=10)
