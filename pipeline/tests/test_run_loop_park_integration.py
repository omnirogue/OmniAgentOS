"""End-to-end, hermetic proof that the REAL bridge/run-loop.sh writes a
correct park marker (and ALERTS.md line) on total seat exhaustion, and
clears it on a successful iteration.

This is the integration seam where a real bug was caught while building this
fix: loop_park.py's CLI originally required --root/--role on the argparse
subcommand token itself, and both run-loop.sh and loop-watchdog.sh pass the
subcommand FIRST — so the wiring silently failed ("the following arguments
are required: --root, --role") until this test exercised the real
heredoc-generated shell, not just the Python functions directly.

Mechanics: run-loop.sh is copied byte-for-byte into a temp "repo" (so its
BASH_SOURCE-derived TL resolves inside the sandbox) alongside loop_park.py
and a throwaway PROMPT-reviewer-loop.md. `tmux` is stubbed on PATH:```
has-session``` always reports "no session", and ```new-session``` executes
the given command line SYNCHRONOUSLY in-process instead of spawning a real
tmux server — nothing here touches the live tmux sessions. The launcher
account is a fake script that always fails, so the retry ladder exhausts in
two iterations without ever calling a real `claude -p`.
"""

import json
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from bridge import loop_park as lp  # noqa: E402 - must follow sys.path.insert above


@pytest.fixture
def sandbox(tmp_path):
    tl = tmp_path / "threeloops"
    (tl / "bridge").mkdir(parents=True)
    (tl / "prompts").mkdir()
    shutil.copy(REPO_ROOT / "bridge" / "run-loop.sh", tl / "bridge" / "run-loop.sh")
    shutil.copy(REPO_ROOT / "bridge" / "loop_park.py", tl / "bridge" / "loop_park.py")
    (tl / "prompts" / "PROMPT-reviewer-loop.md").write_text("test prompt body\n")

    workdir = tmp_path / "workdir"
    (workdir / "var" / "loopqueue" / "state").mkdir(parents=True)
    (workdir / "var" / "loopqueue" / "logs").mkdir(parents=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    # Stub `claude` launchers: always fail, never a real API call. Two of
    # them (primary + fallback) because production ALWAYS invokes
    # run-loop.sh with an explicit fallback (loop-watchdog.sh's `start`
    # calls all name one) — with none given, run-loop.sh auto-populates
    # FALLBACKS from claude1/2/3/6/7 via `command -v`, and on THIS host's
    # stock bash 3.2 an array left genuinely empty then hits a real,
    # pre-existing, out-of-scope latent bug (`${FALLBACKS[*]}: unbound
    # variable` under `set -u`) unrelated to this fix — see the final
    # report. Supplying an explicit fallback, as production always does,
    # sidesteps that path entirely and is the faithful thing to test.
    for name in ("claude-fake", "claude-fake-2"):
        launcher = bin_dir / name
        launcher.write_text("#!/usr/bin/env bash\necho 'stub failure: rc=1' >&2\nexit 1\n")
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)

    # Stub `tmux`: has-session always "no session"; new-session runs the
    # given command line SYNCHRONOUSLY in this same process tree (no real
    # tmux server involved at all) so the test can observe its outcome.
    tmux_stub = bin_dir / "tmux"
    tmux_stub.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  has-session) exit 1 ;;\n"
        '  new-session) eval "${@: -1}"; exit $? ;;\n'
        "  set-option) exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    tmux_stub.chmod(tmux_stub.stat().st_mode | stat.S_IEXEC)

    empty_home = tmp_path / "home"
    empty_home.mkdir()

    return {
        "tl": tl, "workdir": workdir, "bin_dir": bin_dir,
        "loopqueue": workdir / "var" / "loopqueue", "empty_home": empty_home,
    }


def _run(sandbox, extra_env=None):
    env = {
        "PATH": f"{sandbox['bin_dir']}:/usr/bin:/bin",
        "HOME": str(sandbox["empty_home"]),   # clean login shell, no real profile
        "LOOP_WORKDIR": str(sandbox["workdir"]),
        "LOOP_GOV_PY": sys.executable,
        "LOOP_SLEEP": "1",
    }
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [str(sandbox["tl"] / "bridge" / "run-loop.sh"), "reviewer", "claude-fake", "claude-fake-2"],
        env=env, capture_output=True, text=True, timeout=240,  # load-robust under concurrent gate suite
    )
    return proc


def test_seat_exhaustion_writes_marker_and_single_alerts_line(sandbox):
    proc = _run(sandbox)
    # NOTE on exit code: real `tmux new-session -d ...` backgrounds the
    # session and returns immediately with ITS OWN launch-success status —
    # run-loop.sh's outer process always exits 0 after a successful launch,
    # regardless of what the detached session does later. The stub tmux
    # here runs the session body SYNCHRONOUSLY (so this test can observe
    # it), but that does not change what the outer script's own exit code
    # means: 0 here is "launched fine", and the interesting exit code
    # (rc=2, "every seat failed") shows up INSIDE the transcript below,
    # exactly where it does in the real, backgrounded case.
    assert proc.returncode == 0, f"unexpected outer-launch failure: {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "PARKING, alerting once" in proc.stdout

    marker_path = lp.marker_path(sandbox["loopqueue"], "reviewer")
    assert marker_path.exists(), "run-loop.sh did not write a park marker on exhaustion"
    marker = json.loads(marker_path.read_text())
    assert marker["role"] == "reviewer"
    assert marker["seats"] == ["claude-fake", "claude-fake-2"]

    alerts = (sandbox["loopqueue"] / "ALERTS.md").read_text()
    lines = [line for line in alerts.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "reviewer-loop parked" in lines[0]


def test_second_exhaustion_while_still_parked_does_not_add_a_second_alert_line(sandbox):
    first = _run(sandbox)
    assert "PARKING, alerting once" in first.stdout
    second = _run(sandbox)
    # Real production behaviour would never reach this: the watchdog refuses
    # to restart while parked (proved separately in
    # test_loop_watchdog_park_aware.py). This test proves the DEEPER
    # invariant directly on run-loop.sh itself: even if it were re-invoked
    # while still parked, write_park()'s own idempotency keeps the alert
    # line singular.
    assert "PARKING, alerting once" in second.stdout
    alerts = (sandbox["loopqueue"] / "ALERTS.md").read_text()
    lines = [line for line in alerts.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one ALERTS.md line across two exhaustion runs, got: {lines}"


def test_successful_iteration_clears_a_stale_marker(sandbox):
    now = datetime.now(UTC)
    lp.write_park(
        sandbox["loopqueue"], "reviewer", "stale park from a previous run", ["claude-fake"],
        alerts_file=sandbox["loopqueue"] / "ALERTS.md", now=now,
    )
    assert lp.marker_path(sandbox["loopqueue"], "reviewer").exists()

    # A launcher that succeeds once, then keeps failing so run-loop.sh still
    # terminates (park+exit 2) instead of looping forever in this test.
    success_then_fail = sandbox["bin_dir"] / "claude-succeeds-once"
    counter_file = sandbox["bin_dir"] / ".counter"
    success_then_fail.write_text(
        "#!/usr/bin/env bash\n"
        f'n=$(cat "{counter_file}" 2>/dev/null || echo 0)\n'
        f'echo $((n+1)) > "{counter_file}"\n'
        "if [ \"$n\" = \"0\" ]; then echo ok; exit 0; else exit 1; fi\n"
    )
    success_then_fail.chmod(success_then_fail.stat().st_mode | stat.S_IEXEC)

    env = {
        "PATH": f"{sandbox['bin_dir']}:/usr/bin:/bin",
        "HOME": str(sandbox["empty_home"]),
        "LOOP_WORKDIR": str(sandbox["workdir"]),
        "LOOP_GOV_PY": sys.executable,
        "LOOP_SLEEP": "1",
    }
    proc = subprocess.run(
        [str(sandbox["tl"] / "bridge" / "run-loop.sh"), "reviewer", "claude-succeeds-once", "claude-fake"],
        env=env, capture_output=True, text=True, timeout=240,  # load-robust under concurrent gate suite
    )
    assert proc.returncode == 0  # outer launch exit; see note in the exhaustion test above
    assert "iteration end rc=0" in proc.stdout
    # Between the success and the eventual re-park, the ORIGINAL stale
    # marker must have been cleared — the fresh marker's parked_at is not
    # the stale one we seeded above.
    marker = json.loads(lp.marker_path(sandbox["loopqueue"], "reviewer").read_text())
    assert marker["parked_at"] != now.strftime("%Y-%m-%dT%H:%M:%SZ")
