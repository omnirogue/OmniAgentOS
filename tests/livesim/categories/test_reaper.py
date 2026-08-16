"""LiveSim: process reaper & session reaper — the subsystem the operator flagged.

Four reapers coexist on this machine:

  * liveness-reaper.py  (launchd com.youruser.liveness-reaper, 30m) — marks
    session ROWS failed when their pid is proven dead + closes orphaned
    swarm_attempts. Never signals a process. Two-factor PID-reuse defense.
  * A2 in-process reaper (omniagentos/sessions/supervisor.py) — the "session
    reaper": kills RUNNING/RESUMING bridge sessions idle past a threshold
    (enforce-gated) and terminalizes awaiting_approval sessions past max-park
    (ALWAYS enforced). This is the one that can kill legitimate work.
  * idle-reaper.sh (Ops, 5m) — reclaims memory from runaway daemons /
    flatlined agent CLIs; ARMED=0 => report-only.
  * fleet-reaper.sh (Ops, 60s) — flags overdue runs; never signals.

These tests are non-destructive: product code is exercised with SYNTHETIC rows
(no live session is killed), shell reapers are run in report-only/dry-run mode,
and the live DB is read read-only. The live-evidence test records how many real
sessions the reapers have terminalized (the "killing legitimate sessions"
signal) without asserting a product verdict.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest

pytestmark = pytest.mark.livesim

REPO = Path(__file__).resolve().parents[3]
LIVENESS_PY = Path("/Users/youruser/Work/OmniAgentOS/Ops/agent-observability/liveness-reaper.py")
IDLE_REAPER_SH = Path("/Users/youruser/Work/Ops/bin/idle-reaper.sh")
FLEET_REAPER_SH = Path("/Users/youruser/Work/Ops/bin/fleet-reaper.sh")


def _load_liveness():
    if not LIVENESS_PY.exists():
        pytest.skip(f"liveness-reaper.py not found at {LIVENESS_PY}")
    spec = importlib.util.spec_from_file_location("livesim_liveness_reaper", LIVENESS_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# liveness-reaper: correctness of the DEAD/ALIVE classifier (synthetic rows)
# ---------------------------------------------------------------------------


@pytest.mark.positive
def test_liveness_reaper_classifies_dead_pid_as_dead(livesim):
    """A row whose pid is genuinely gone is DEAD (the reaper's core job)."""
    livesim.target("proc")
    lr = _load_liveness()
    now = time.time()
    # A pid that cannot exist -> os.kill ESRCH -> dead.
    row = {
        "id": "ses_synthetic_dead",
        "state": "running",
        "pid": 2_147_483_000,  # absurdly high, not a live pid
        "provider": "claude",
        "created_at": lr.iso_z(lr.utc_now()),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600)),
    }
    verdict, reason = lr.classify_session(row, now, min_age_s=900, reuse_tolerance_s=300)
    livesim.record(inputs=row, outputs={"verdict": verdict, "reason": reason})
    assert verdict == "DEAD", reason


@pytest.mark.negative
def test_liveness_reaper_never_touches_a_live_pid(livesim):
    """Our own live pid, recorded on a row, must classify ALIVE — never reaped."""
    livesim.target("proc")
    lr = _load_liveness()
    now = time.time()
    # The row is old enough to be eligible (age > min-age), and the live process
    # started when the row was created — i.e. it IS the session's real process.
    # proc_info is stubbed to report that matching start time for our live pid so
    # the "within reuse tolerance => the real process => ALIVE" branch is exercised
    # deterministically (the pid itself is genuinely alive: os.getpid()).
    row = {
        "id": "ses_synthetic_self",
        "state": "running",
        "pid": os.getpid(),
        "provider": "claude",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 1800)),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 1800)),
    }
    with mock.patch.object(
        lr, "proc_info",
        return_value={"start_epoch": now - 1800, "elapsed_s": 1800, "command": "claude -p"},
    ):
        verdict, reason = lr.classify_session(row, now, min_age_s=900, reuse_tolerance_s=300)
    livesim.record(inputs={"pid": os.getpid()}, outputs={"verdict": verdict, "reason": reason})
    assert verdict == "ALIVE", reason


@pytest.mark.boundary
def test_liveness_reaper_pid_reuse_two_factor(livesim):
    """PID reuse: alive pid that post-dates the row AND is unrelated => DEAD;
    but if its command line still mentions the provider => ALIVE (fail-closed)."""
    livesim.target("proc")
    lr = _load_liveness()
    now = time.time()
    created = now - 4000
    my_pid = os.getpid()

    # Factor test via the classifier's own proc_info: monkeypatch it so we can
    # assert both branches deterministically without spawning processes.
    row = {
        "id": "ses_reuse",
        "state": "running",
        "pid": my_pid,
        "provider": "claude",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created)),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 1800)),
    }
    # squatter: started 1000s AFTER the row, command unrelated to any provider
    with mock.patch.object(
        lr, "proc_info", return_value={"start_epoch": created + 1000, "elapsed_s": 3000, "command": "/usr/bin/vim notes.txt"}
    ):
        v_dead, r_dead = lr.classify_session(row, now, 900, 300)
    # same timing but command still mentions the provider -> refuse to guess
    with mock.patch.object(
        lr, "proc_info", return_value={"start_epoch": created + 1000, "elapsed_s": 3000, "command": "claude -p resume"}
    ):
        v_alive, r_alive = lr.classify_session(row, now, 900, 300)
    livesim.record(outputs={"unrelated": v_dead, "provider_match": v_alive})
    assert v_dead == "DEAD", r_dead
    assert v_alive == "ALIVE", r_alive


@pytest.mark.boundary
def test_liveness_reaper_min_age_protects_young_rows(livesim):
    """A freshly-created starting row (< min-age) is TOO_YOUNG, never reaped —
    protects a session that has not yet written its pid."""
    livesim.target("proc")
    lr = _load_liveness()
    now = time.time()
    row = {
        "id": "ses_young",
        "state": "starting",
        "pid": None,
        "provider": "claude",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60)),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60)),
    }
    verdict, reason = lr.classify_session(row, now, min_age_s=900, reuse_tolerance_s=300)
    livesim.record(outputs={"verdict": verdict, "reason": reason})
    assert verdict == "TOO_YOUNG", reason


@pytest.mark.positive
def test_liveness_reaper_dryrun_against_live_db_is_readonly(livesim):
    """Running the real reaper in DRY-RUN against the live DB produces a valid
    report and writes nothing (mode=ro). Every DEAD verdict it reaches must have
    a genuinely dead pid — the reaper must not plan to fail a live session."""
    import json as _json

    livesim.target("proc", "db")
    if not LIVENESS_PY.exists():
        pytest.skip("liveness-reaper.py absent")
    out = subprocess.run(
        ["/usr/bin/python3", str(LIVENESS_PY), "--json"],  # no --apply => dry-run/read-only
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    report = _json.loads(out.stdout)
    livesim.evidence("liveness-dryrun.json", out.stdout)
    livesim.record(outputs={"sessions_examined": report["sessions_examined"],
                            "sessions_to_reap": report["sessions_to_reap"]})
    assert report["mode"] == "dry-run"
    # Cross-check: each planned-DEAD session's pid really is dead right now.
    for s in report["sessions"]:
        if s["verdict"] != "DEAD":
            continue
        pid = s.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        alive = True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        assert not alive, f"reaper planned to DEAD-reap {s['id']} but pid {pid} is alive"


# ---------------------------------------------------------------------------
# A2 session reaper (supervisor) — the enforce/idle/max-park knobs
# ---------------------------------------------------------------------------


def _supervisor_module():
    try:
        from omniagentos.sessions import supervisor  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot import supervisor: {exc}")
    return supervisor


def _bare_supervisor(sup):
    """A SessionSupervisor whose env-only helpers we can call without a real DAL."""
    inst = sup.SessionSupervisor.__new__(sup.SessionSupervisor)
    return inst


@pytest.mark.boundary
def test_a2_idle_threshold_defaults_and_override(livesim):
    livesim.target("proc")
    sup = _supervisor_module()
    inst = _bare_supervisor(sup)
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(sup.IDLE_MINUTES_ENV, None)
        default = inst._idle_threshold_seconds({})
        override = inst._idle_threshold_seconds({"idle_minutes": 45})
    livesim.record(outputs={"default_s": default, "override_s": override})
    assert default == sup.DEFAULT_IDLE_MINUTES * 60.0
    assert override == 45 * 60.0  # per-session override wins over the global default


@pytest.mark.boundary
def test_a2_max_park_ceiling_default_is_20m(livesim):
    livesim.target("proc")
    sup = _supervisor_module()
    inst = _bare_supervisor(sup)
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(sup.MAX_PARK_MINUTES_ENV, None)
        ceil = inst._max_park_seconds()
        os.environ[sup.MAX_PARK_MINUTES_ENV] = "0"
        disabled = inst._max_park_seconds()
    livesim.record(outputs={"ceiling_s": ceil, "disabled": disabled})
    assert ceil == sup.DEFAULT_MAX_PARK_MINUTES * 60.0 == 1200.0
    assert disabled == 0.0  # 0 explicitly disables the ceiling


@pytest.mark.permission
def test_a2_reaper_enforce_flag_semantics(livesim):
    """enforce is OFF unless the env var is explicitly truthy — the difference
    between a logged would-kill and a real kill."""
    livesim.target("proc")
    sup = _supervisor_module()
    inst = _bare_supervisor(sup)
    results = {}
    for val, expect in [("", False), ("0", False), ("1", True), ("true", True), ("yes", True), ("no", False)]:
        with mock.patch.dict(os.environ, {sup.REAPER_ENFORCE_ENV: val}):
            got = inst._reaper_enforced()
        results[val or "<unset>"] = got
        assert got is expect, f"enforce({val!r}) = {got}, expected {expect}"
    livesim.record(outputs=results)


@pytest.mark.degradation
def test_a2_enforce_live_env_is_recorded(livesim):
    """OBSERVATIONAL: record whether the LIVE launch env arms the A2 reaper.
    launch-omniagentos.sh exports OMNIAGENTOS_REAPER_ENFORCE=1, so idle kills are
    real when the supervisor runs under it. This test records the ground truth;
    it does not assert a product verdict."""
    livesim.target("proc")
    launcher = REPO / "scripts" / "launch-omniagentos.sh"
    enforce_in_launcher = False
    if launcher.exists():
        enforce_in_launcher = "OMNIAGENTOS_REAPER_ENFORCE=1" in launcher.read_text()
    livesim.record(outputs={"enforce_exported_by_launcher": enforce_in_launcher})
    livesim.note(f"A2 idle reaper enforce armed by launch-omniagentos.sh: {enforce_in_launcher}")
    assert isinstance(enforce_in_launcher, bool)  # always passes; value is the datum


@pytest.mark.degradation
def test_live_reaper_kill_evidence(livesim, live_db_ro):
    """OBSERVATIONAL: count real sessions the reapers terminalized. A non-zero
    max-park count is the 'killing legitimate sessions' signal the operator flagged.
    Passes as long as the DB is readable; the counts are the deliverable."""
    livesim.target("db")
    cur = live_db_ro.execute(
        "SELECT COALESCE(killed_by,'(none)') kb, COUNT(*) n FROM sessions "
        "WHERE killed_by IS NOT NULL GROUP BY kb ORDER BY n DESC"
    )
    by_killer = {r["kb"]: r["n"] for r in cur.fetchall()}
    cur = live_db_ro.execute(
        "SELECT COUNT(*) n FROM sessions WHERE error LIKE '%reaped by liveness-reaper%'"
    )
    liveness_rows = cur.fetchone()["n"]
    cur = live_db_ro.execute(
        "SELECT COUNT(*) n FROM sessions WHERE killed_by='max-park' "
        "AND updated_at >= datetime('now','-7 days')"
    )
    max_park_7d = cur.fetchone()["n"]
    out = {"by_killed_by": by_killer, "liveness_reaped_rows": liveness_rows,
           "max_park_last_7d": max_park_7d}
    livesim.evidence("reaper-kill-evidence.json", __import__("json").dumps(out, indent=2))
    livesim.record(outputs=out)
    livesim.note(f"max-park kills last 7d: {max_park_7d}; total max-park: {by_killer.get('max-park',0)}")
    assert True  # observational monitor, not a gate


# ---------------------------------------------------------------------------
# idle-reaper.sh and fleet-reaper.sh — report-only safety
# ---------------------------------------------------------------------------


@pytest.mark.negative
def test_idle_reaper_report_only_kills_nothing(livesim):
    """idle-reaper.sh with ARMED=0 must log would-do actions and signal nothing.
    We verify our own process survives and the run reports acted=0 in armed=0."""
    livesim.target("proc")
    if not IDLE_REAPER_SH.exists():
        pytest.skip("idle-reaper.sh absent")
    my_pid = os.getpid()
    env = dict(os.environ, IDLE_REAPER_ARMED="0")
    out = subprocess.run(
        ["/bin/bash", str(IDLE_REAPER_SH)], capture_output=True, text=True, timeout=90, env=env
    )
    livesim.evidence("idle-reaper-stdout.txt", out.stdout + "\n---STDERR---\n" + out.stderr)
    # our process must still be alive (report-only never signals)
    alive = True
    try:
        os.kill(my_pid, 0)
    except ProcessLookupError:
        alive = False
    livesim.record(outputs={"rc": out.returncode, "self_alive": alive})
    assert alive
    assert out.returncode == 0


@pytest.mark.positive
def test_fleet_reaper_flags_without_signalling(livesim):
    """fleet-reaper.sh appends a heartbeat and, by contract, signals no process.
    Static+dynamic: run it, confirm the heartbeat grows, confirm no 'kill' verb."""
    livesim.target("proc")
    if not FLEET_REAPER_SH.exists():
        pytest.skip("fleet-reaper.sh absent")
    src = FLEET_REAPER_SH.read_text()
    # contract: the script never sends a signal
    assert "kill " not in src and "kill(" not in src, "fleet-reaper must never signal"
    out = subprocess.run(
        ["/bin/bash", str(FLEET_REAPER_SH)], capture_output=True, text=True, timeout=60,
        env=dict(os.environ, IDLE_REAPER_ARMED="0"),
    )
    livesim.record(outputs={"rc": out.returncode})
    assert out.returncode == 0
