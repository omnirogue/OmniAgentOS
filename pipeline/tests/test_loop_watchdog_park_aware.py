"""End-to-end, hermetic proof that loop-watchdog.sh itself honours the park
marker — not just the underlying loop_park.py library.

This runs the REAL bridge/loop-watchdog.sh (copied byte-for-byte into a temp
"repo" so BASH_SOURCE-derived paths resolve inside the sandbox) sourced with
LOOP_WATCHDOG_SOURCE_ONLY=1, against:
  - a stub `tmux` on PATH that always reports "no session" (so start()
    proceeds past its early-return),
  - a stub run-loop.sh in the copied bridge/ dir that just records that it
    was invoked (never a real launch),
  - a temp LOOP_WORKDIR with its own var/loopqueue.

Nothing here touches the live tmux server, spawns `claude -p`, or writes to
the live var/loopqueue/ALERTS.md.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from bridge import loop_park as lp  # noqa: E402 - must follow sys.path.insert above


@pytest.fixture
def sandbox(tmp_path):
    """Builds the temp TL (ThreeLoops-shaped) tree + temp WORKDIR + stub PATH."""
    tl = tmp_path / "threeloops"
    (tl / "bridge").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "bridge" / "loop-watchdog.sh", tl / "bridge" / "loop-watchdog.sh")
    shutil.copy(REPO_ROOT / "bridge" / "loop_park.py", tl / "bridge" / "loop_park.py")

    workdir = tmp_path / "workdir"
    loopqueue = workdir / "var" / "loopqueue"
    (loopqueue / "state").mkdir(parents=True)
    (loopqueue / "logs").mkdir(parents=True)

    invoked_marker = tmp_path / "run-loop-invoked.marker"
    run_loop_stub = tl / "bridge" / "run-loop.sh"
    run_loop_stub.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"$(date -u +%Y-%m-%dT%H:%M:%SZ) role=$1 seats=$*\" >> {invoked_marker}\n"
    )
    run_loop_stub.chmod(run_loop_stub.stat().st_mode | stat.S_IEXEC)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tmux_stub = bin_dir / "tmux"
    # `tmux has-session` must EXIT NONZERO (no session) so start() proceeds
    # past its early-return and reaches the park check.
    tmux_stub.write_text("#!/usr/bin/env bash\nexit 1\n")
    tmux_stub.chmod(tmux_stub.stat().st_mode | stat.S_IEXEC)

    return {
        "tl": tl,
        "workdir": workdir,
        "loopqueue": loopqueue,
        "bin_dir": bin_dir,
        "invoked_marker": invoked_marker,
    }


def _run_start(sandbox, role, real_python=sys.executable):
    """Sources the real loop-watchdog.sh (LOOP_WATCHDOG_SOURCE_ONLY=1) and
    calls its start() function once for `role`, returning (stdout, rc)."""
    env = dict(os.environ)
    env["LOOP_WATCHDOG_SOURCE_ONLY"] = "1"
    env["LOOP_WORKDIR"] = str(sandbox["workdir"])
    env["LOOP_GOV_PY"] = real_python
    env["PATH"] = f"{sandbox['bin_dir']}:{env.get('PATH', '')}"
    script = f'source "{sandbox["tl"]}/bridge/loop-watchdog.sh"; start {role} claude-fake-seat'
    proc = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=120,  # load-robust under concurrent gate suite
    )
    return proc.stdout + proc.stderr, proc.returncode


# ---------------------------------------------------------------------------
# item (b) — UNEXPIRED marker: run-loop.sh must NOT be invoked.
# ---------------------------------------------------------------------------

def test_unexpired_marker_blocks_watchdog_from_invoking_run_loop(sandbox):
    now = datetime.now(UTC)
    lp.write_park(
        sandbox["loopqueue"], "reviewer", "every seat failed", ["claude6", "claude4"],
        alerts_file=sandbox["loopqueue"] / "ALERTS.md", now=now,
    )
    out, rc = _run_start(sandbox, "reviewer")
    assert not sandbox["invoked_marker"].exists(), (
        f"run-loop.sh WAS invoked while an unexpired park marker existed: {out}"
    )
    assert "parked" in out.lower() and "refusing" in out.lower()


# ---------------------------------------------------------------------------
# item (c) — EXPIRED marker: run-loop.sh IS invoked, and a fresh success
# clears the marker (clear_park is the actual clearer; this proves the
# watchdog side of "expired -> restart permitted").
# ---------------------------------------------------------------------------

def test_expired_marker_lets_watchdog_invoke_run_loop(sandbox):
    now = datetime.now(UTC) - timedelta(hours=1)
    lp.write_park(
        sandbox["loopqueue"], "reviewer", "every seat failed", ["claude6"],
        alerts_file=sandbox["loopqueue"] / "ALERTS.md", now=now,
    )
    marker = json.loads(lp.marker_path(sandbox["loopqueue"], "reviewer").read_text())
    until = lp._parse_ts(marker["until"])
    assert until < datetime.now(UTC), "fixture bug: marker must already be expired"

    out, rc = _run_start(sandbox, "reviewer")
    assert sandbox["invoked_marker"].exists(), f"run-loop.sh was NOT invoked for an expired park: {out}"
    assert "restarting" in out.lower()
    assert "expired" in out.lower()


def test_no_marker_at_all_lets_watchdog_invoke_run_loop_silently(sandbox):
    out, rc = _run_start(sandbox, "reviewer")
    assert sandbox["invoked_marker"].exists()
    assert "restarting" in out.lower()


# ---------------------------------------------------------------------------
# refusal logged once per park across repeated watchdog TICKS (not just
# once per check_park() call in isolation — proves the bash wiring too).
# ---------------------------------------------------------------------------

def test_watchdog_logs_refusal_once_across_three_ticks(sandbox):
    now = datetime.now(UTC)
    lp.write_park(
        sandbox["loopqueue"], "reviewer", "every seat failed", ["claude6"],
        alerts_file=sandbox["loopqueue"] / "ALERTS.md", now=now,
    )
    outputs = [_run_start(sandbox, "reviewer")[0] for _ in range(3)]
    assert not sandbox["invoked_marker"].exists()
    logged = [o for o in outputs if "refusing" in o.lower()]
    assert len(logged) == 1, f"expected exactly one refusal log line across 3 ticks, got: {outputs}"

