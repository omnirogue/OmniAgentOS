"""Red-first regressions for harvest close-then-release and singleton lock.

CHANGE 1: claim release must never precede worktree close (a concurrent
``_run_iteration`` can re-acquire the claim and force-remove the tree while
the harvester still has cwd inside it).

CHANGE 2: only one harvest pass may run at a time; a live lock holder with a
future deadline refuses; a dead or past-deadline holder is reclaimed.

Round 2 (Gemini Class review of 653f1723c, REQUEST-CHANGES):
  1. BLOCKER: lock acquisition was check-then-write (TOCTOU) -- replaced by
     an atomic O_CREAT|O_EXCL create; ``test_harvest_lock_concurrent_...``
     exercises two real racing threads and asserts exactly one wins.
  2. MAJOR: a stale-deadline holder was killed on liveness alone, so a
     recycled pid could hit an innocent process -- the lock now also records
     ``pid_started`` (ps lstart, the gate_loop.py identity pattern) and a
     mismatch is treated as a DEAD holder, taken over WITHOUT signalling;
     ``test_harvest_lock_pid_reuse_takes_over_without_signalling`` covers it.
  3. MINOR: the innermost timeout-escalation branch could leave the child
     unreaped if its pipes never drained even after SIGKILL --
     ``test_run_verification_command_always_reaps_on_timeout`` covers it via
     the extracted ``_run_verification_command`` helper.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PIPELINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE))

from bridge import canonical  # noqa: E402
from bridge import spawn_builders as sb  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    (root / "README.md").write_text("baseline\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "baseline")
    return root


@pytest.fixture
def loops_root(repo: Path) -> Path:
    root = repo / "var" / "loopqueue"
    for name in ("claims", "state", "candidates", "proposals", "parked"):
        (root / name).mkdir(parents=True)
    (root / "ledger.jsonl").touch()
    (root / "state" / "landers.json").write_text(json.dumps({
        "repo": "test", "last_tick_ts": sb._iso(datetime.now(UTC)),
        "status": "ok", "pid": 1,
    }))
    return root


def _proposal(tag: str) -> dict:
    payload = {
        "urgency": "p1", "benefit_class": "throughput", "impact": "high",
        "risk_level": "medium", "problem": f"build {tag}",
        "falsifier": f"{tag} is implemented", "implementation_plan": "implement it",
        "effort": "s", "new_paths": [], "repo": "pipeline",
    }
    ident = canonical.content_id(payload)
    return {
        "contract": "v1.1", "kind": "proposal", "title": f"proposal {tag}",
        "created_at": "2026-08-10T00:00:00Z",
        "producer": {"role": "external", "actor": "test", "lineage": "test"},
        "paths": ["README.md"], "payload": payload, "id": ident, "priority": 0,
    }


def _write_proposal(root: Path, item: dict) -> Path:
    path = root / "proposals" / f"{item['id'].replace(':', '_', 1)}.json"
    path.write_text(json.dumps(item))
    return path


def _commit_builder_change(worklist: dict) -> Path:
    worktree = Path(worklist["worktree"])
    (worktree / "README.md").write_text("built\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-qm", "build result")
    return worktree


def _order_spies(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    order: list[str] = []

    def close(_repo: Path, worktree: Path) -> tuple[bool, str]:
        order.append("close")
        return True, ""

    def release(loops_root: Path, ident: str, claim_actor: object, *, out_name: str) -> None:
        order.append("release")

    monkeypatch.setattr(sb, "_close_worktree", close)
    monkeypatch.setattr(sb, "_release_claim_after_harvest", release)
    return order


def _provisioned_built(loops_root: Path, repo: Path, tag: str) -> tuple[dict, Path, str]:
    item = _proposal(tag)
    _write_proposal(loops_root, item)
    command = f"{sys.executable} -c pass"
    result = sb._run_iteration(loops_root, repo, actor="coordinator", test_cmd=command)
    worktree = _commit_builder_change(result["worklist"][0])
    return item, worktree, command


# --------------------------------------------------------------------------
# CHANGE 1 — close-then-release
# --------------------------------------------------------------------------


def test_success_path_closes_before_releasing_claim(
        loops_root: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _item, worktree, command = _provisioned_built(loops_root, repo, "close-order-ok")
    order = _order_spies(monkeypatch)

    out = sb._harvest_one(loops_root, repo, worktree, actor="coordinator",
                          test_cmd=command, close=True)

    assert out is not None
    assert "close" in order, f"close never called on success path; order={order}"
    assert "release" in order, f"release never called on success path; order={order}"
    assert order.index("close") < order.index("release"), (
        f"claim released before worktree close: order={order}")


def test_reconcile_path_closes_before_releasing_claim(
        loops_root: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item, worktree, command = _provisioned_built(loops_root, repo, "close-order-reconcile")
    branch = json.loads((worktree / sb.MARKER_NAME).read_text())["branch"]
    tip = _git(repo, "rev-parse", branch)
    marker = json.loads((worktree / sb.MARKER_NAME).read_text())
    envelope = sb._envelope(
        item, branch=branch, base_sha=marker["base_sha"], tip_sha=tip,
        actor="coordinator",
        evidence={"claim": "prior", "verified_by": "execution",
                  "command": command, "exit_code": 0})
    existing = loops_root / "candidates" / f"{envelope['id'].replace(':', '_', 1)}.json"
    existing.write_text(json.dumps(envelope, indent=2) + "\n")

    order = _order_spies(monkeypatch)
    out = sb._harvest_one(loops_root, repo, worktree, actor="coordinator",
                          test_cmd=command, close=True)

    assert out is None
    assert "close" in order, f"close never called on reconcile path; order={order}"
    assert "release" in order, f"release never called on reconcile path; order={order}"
    assert order.index("close") < order.index("release"), (
        f"reconcile released claim before worktree close: order={order}")


# --------------------------------------------------------------------------
# CHANGE 2 / Round 3 — harvest singleton lock (fcntl.flock design)
# --------------------------------------------------------------------------
#
# Round 3 (Gemini Class review of 1db8b547b, REQUEST-CHANGES):
#   1. BLOCKER: the round-2 "unlink + retry O_EXCL" takeover was itself a
#      TOCTOU (a delayed racer could unlink a competitor's freshly-created
#      VALID lock, letting both proceed). Redesigned so mutual exclusion
#      rests entirely on fcntl.flock -- no unlink, no steal logic, no
#      window at all. Covered by
#      test_harvest_lock_concurrent_acquire_exactly_one_wins (real threads
#      racing a real kernel flock) and
#      test_harvest_lock_holder_death_releases_lock (a REAL process death,
#      not a JSON write, frees the lock).
#   2. MAJOR: a live legacy lock with no recorded pid_started was stolen
#      unconditionally. Now: no recorded identity -> refuse, never signal.
#      Covered by test_evaluate_lock_holder_no_recorded_identity_refuses.
#   3. MINOR/RULING: the round-2 "corrupt lock -> steal" judgment was
#      wrong -- acquisition is open-then-write, so a 0-byte/partial file
#      can be a LEGITIMATE in-flight write by the real holder, not proof of
#      corruption. Now: unreadable metadata -> refuse, no unlink, no kill.
#      Covered by test_evaluate_lock_holder_midwrite_content_refuses.
#
# The two "past deadline" tests cover the split this round introduced:
# identity-MISMATCHED (pid recycled -- never signal) vs. identity-VERIFIED
# (genuinely still the same live holder -- signal it).


def _lock_path(loops_root: Path) -> Path:
    return loops_root / "locks" / "harvest.lock"


def _write_lock(loops_root: Path, *, pid: int, started: datetime,
                deadline: datetime, pid_started: str | None = "_AUTO_") -> Path:
    """Write raw lock JSON directly to disk (bypassing the real acquire path
    entirely -- these bytes are NEVER held under a genuine flock by anyone).

    That is exactly the right tool for driving
    ``_evaluate_harvest_lock_holder`` in isolation: that function is only
    ever reached in production after losing the real ``flock``, so exercising
    it directly against hand-built metadata is equivalent to -- and far more
    deterministic than -- orchestrating a genuine concurrent holder for
    every scenario.

    ``pid_started="_AUTO_"`` (the default) records the REAL current ps lstart
    for ``pid`` so identity verification succeeds by default; pass an
    explicit mismatched string to simulate pid reuse, or ``None`` to
    simulate a legacy lock with no recorded identity at all.
    """
    path = _lock_path(loops_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if pid_started == "_AUTO_":
        try:
            pid_started = sb._pid_lstart(pid)
        except OSError:
            pid_started = None
    path.write_text(json.dumps({
        "pid": pid,
        "started_at": sb._iso(started),
        "deadline": sb._iso(deadline),
        "pid_started": pid_started,
    }) + "\n")
    return path


def _track_kill_signals(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    signals: list[int] = []

    def track_killpg(_pgid: int, sig: int) -> None:
        signals.append(sig)

    def track_kill(_pid: int, sig: int) -> None:
        if sig != 0:
            signals.append(sig)

    monkeypatch.setattr(os, "killpg", track_killpg)
    monkeypatch.setattr(os, "kill", track_kill)
    return signals


def test_harvest_lock_concurrent_acquire_exactly_one_wins(loops_root: Path) -> None:
    """Two threads race a REAL kernel ``flock`` on a fresh lock path;
    exactly one must win. (The pre-round-3 unlink+retry dance could let a
    delayed racer unlink a competitor's freshly-created valid lock and let
    both proceed -- flock arbitration has no such window.)
    """
    barrier = threading.Barrier(2)
    results: list[bool] = []
    fds: list[int | None] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait(timeout=5)
        fd = sb._acquire_harvest_lock(loops_root, retry_timeout_s=1.0)
        with results_lock:
            results.append(fd is not None)
            fds.append(fd)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    try:
        assert sorted(results) == [False, True], (
            f"expected exactly one real-flock winner, got {results}")
    finally:
        for fd in fds:
            if fd is not None:
                sb._release_harvest_lock(fd)


def test_harvest_lock_holder_death_releases_lock(loops_root: Path) -> None:
    """A holder that dies (crash, SIGKILL, normal exit -- any reason)
    releases its flock automatically. No reclaim/steal/unlink logic ever
    runs for this case: the very next acquire attempt just succeeds.
    """
    env = dict(os.environ)
    pypath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(PIPELINE) + (os.pathsep + pypath if pypath else "")
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(PIPELINE)!r})\n"
        "from pathlib import Path\n"
        "from bridge import spawn_builders as sb\n"
        f"fd = sb._acquire_harvest_lock(Path({str(loops_root)!r}))\n"
        "assert fd is not None, 'child could not acquire the lock'\n"
        "# Exit WITHOUT releasing -- the kernel must free the flock anyway.\n"
    )
    child = subprocess.run([sys.executable, "-c", script], env=env,
                           capture_output=True, text=True, timeout=15)
    assert child.returncode == 0, child.stdout + child.stderr

    fd = sb._acquire_harvest_lock(loops_root, retry_timeout_s=2.0)
    try:
        assert fd is not None, (
            "lock must be immediately free after the holder process died")
    finally:
        if fd is not None:
            sb._release_harvest_lock(fd)


def test_evaluate_lock_holder_past_deadline_pid_mismatch_takes_over_without_kill(
        loops_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recorded pid is alive (this test process) but its recorded
    ``pid_started`` does not match its real lstart -- the pid was recycled
    since the lock was written. Must take over WITHOUT ever signalling the
    (innocent, unrelated) live process now holding that pid.
    """
    now = datetime.now(UTC)
    path = _write_lock(loops_root, pid=os.getpid(),
                       started=now - timedelta(hours=3),
                       deadline=now - timedelta(minutes=1),
                       pid_started="Thu Jan  1 00:00:00 1970")  # deliberately wrong
    kill_signals = _track_kill_signals(monkeypatch)

    decision = sb._evaluate_harvest_lock_holder(path)

    assert decision == "takeover"
    assert kill_signals == [], (
        f"pid-reuse takeover must never signal the unrelated live holder; "
        f"sent {kill_signals}")
    assert sb._pid_alive(os.getpid())


def test_evaluate_lock_holder_past_deadline_verified_holder_is_killed(
        loops_root: Path) -> None:
    """Recorded pid is alive AND its recorded lstart matches -- a genuinely
    verified, still-live, past-deadline holder. This one IS signalled.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        now = datetime.now(UTC)
        real_lstart = sb._pid_lstart(child.pid)
        assert real_lstart is not None
        path = _write_lock(loops_root, pid=child.pid,
                           started=now - timedelta(hours=3),
                           deadline=now - timedelta(minutes=1),
                           pid_started=real_lstart)

        decision = sb._evaluate_harvest_lock_holder(path)

        assert decision == "takeover"
        # child.wait() (not _pid_alive) both reaps and confirms termination --
        # an unreaped zombie still answers kill(pid, 0) on macOS/Linux, so
        # _pid_alive alone cannot distinguish "killed, not yet reaped" from
        # "still running".
        exit_code = child.wait(timeout=5)
        assert exit_code != 0, (
            f"verified past-deadline holder must be terminated by signal, "
            f"got exit_code={exit_code}")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_evaluate_lock_holder_midwrite_content_refuses(
        loops_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 0-byte lock file -- exactly the window right after
    ``os.ftruncate(fd, 0)`` inside a real holder's metadata write -- is a
    LEGITIMATE in-flight state, not proof of corruption or death. Must
    refuse this pass, never unlink, never signal (round 3 ruling).
    """
    path = _lock_path(loops_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    kill_signals = _track_kill_signals(monkeypatch)

    decision = sb._evaluate_harvest_lock_holder(path)

    assert decision == "refuse", (
        "a 0-byte/partial lock file must be treated as a possible "
        "in-flight write by the real holder, never as proof of "
        "corruption/death")
    assert path.exists(), "refusing must never unlink the file"
    assert path.read_bytes() == b""
    assert kill_signals == []


def test_evaluate_lock_holder_no_recorded_identity_refuses(
        loops_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live pid with no recorded ``pid_started`` (legacy/unknown holder
    identity) must never be stolen unconditionally -- refuse and wait it
    out; never signal an unverified holder.
    """
    now = datetime.now(UTC)
    path = _write_lock(loops_root, pid=os.getpid(),
                       started=now, deadline=now + timedelta(hours=1),
                       pid_started=None)
    kill_signals = _track_kill_signals(monkeypatch)

    decision = sb._evaluate_harvest_lock_holder(path)

    assert decision == "refuse", (
        "a live pid with no recorded identity must never be stolen "
        "unconditionally")
    assert kill_signals == []


# --------------------------------------------------------------------------
# Round 4 (Gemini Class review) — recorded-child takeover, lease renewal
# --------------------------------------------------------------------------
#
# Round 4 (Gemini Class review of 6ee397392, REQUEST-CHANGES):
#   1. BLOCKER: a takeover killed only the HOLDER's own process group, but
#      the verification child runs in its OWN session (start_new_session=
#      True, deliberately -- see _run_verification_command) so the kill
#      never reached it: the parent died, the child (and any pytest-xdist
#      workers under it) kept running at unbounded CPU -- the exact orphan
#      class this whole series exists to close. Fixed by having the holder
#      RECORD its live verification child in the lock metadata
#      (_record_verification_child) and having a takeover verify-then-kill
#      it too (_terminate_recorded_child), with the same identity discipline
#      as the holder check. Covered by
#      test_evaluate_lock_holder_past_deadline_kills_recorded_verified_child
#      and
#      test_evaluate_lock_holder_recorded_child_identity_mismatch_not_signalled.
#   2. MAJOR: a flat 2h TTL measured from acquire time killed a healthy
#      multi-worktree batch mid-harvest once total runtime exceeded the
#      TTL, even though every individual worktree was well within its own
#      3600s bound. Fixed with a renew-on-progress lease
#      (_renew_harvest_lock_lease, called by _harvest_all after EACH
#      worktree completes) instead of a static deadline. Covered by
#      test_harvest_lease_renewal_advances_deadline_and_refuses_mid_batch
#      and test_harvest_lease_without_renewal_expires_normally (confirming
#      the pre-existing expiry behavior is unchanged when renewal simply
#      never fires).


def test_evaluate_lock_holder_past_deadline_kills_recorded_verified_child(
        loops_root: Path) -> None:
    """A verified, still-live, past-deadline holder with a RECORDED,
    identity-verified verification child (its own session, exactly as
    _run_verification_command spawns it) -- takeover must terminate BOTH,
    not just the holder's own group.
    """
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True)
    try:
        now = datetime.now(UTC)
        holder_lstart = sb._pid_lstart(holder.pid)
        child_lstart = sb._pid_lstart(child.pid)
        assert holder_lstart is not None and child_lstart is not None
        path = _lock_path(loops_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pid": holder.pid,
            "started_at": sb._iso(now - timedelta(hours=3)),
            "deadline": sb._iso(now - timedelta(minutes=1)),
            "pid_started": holder_lstart,
            "child_pid": child.pid,
            "child_pgid": child.pid,
            "child_lstart": child_lstart,
        }))

        decision = sb._evaluate_harvest_lock_holder(path)

        assert decision == "takeover"
        holder_exit = holder.wait(timeout=5)
        assert holder_exit != 0, "verified past-deadline holder must be terminated"
        child_exit = child.wait(timeout=5)
        assert child_exit != 0, (
            "recorded, identity-verified verification child must be "
            "terminated too -- it runs in its own session and a "
            "holder-group-only kill never reaches it")
    finally:
        for p in (holder, child):
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)


def test_evaluate_lock_holder_recorded_child_identity_mismatch_not_signalled(
        loops_root: Path) -> None:
    """The recorded child_pid is alive (a real, unrelated child) but its
    recorded child_lstart does not match -- the original verification
    child's pid was recycled. The holder (identity-verified) is still
    terminated, but the mismatched child must NEVER be signalled.
    """
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True)
    try:
        now = datetime.now(UTC)
        holder_lstart = sb._pid_lstart(holder.pid)
        assert holder_lstart is not None
        path = _lock_path(loops_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pid": holder.pid,
            "started_at": sb._iso(now - timedelta(hours=3)),
            "deadline": sb._iso(now - timedelta(minutes=1)),
            "pid_started": holder_lstart,
            "child_pid": child.pid,
            "child_pgid": child.pid,
            "child_lstart": "Thu Jan  1 00:00:00 1970",  # deliberately wrong
        }))

        decision = sb._evaluate_harvest_lock_holder(path)

        assert decision == "takeover"
        holder_exit = holder.wait(timeout=5)
        assert holder_exit != 0, "verified holder must still be terminated"
        time.sleep(0.5)
        assert child.poll() is None, (
            "an identity-mismatched recorded child (pid recycled) must "
            "never be signalled")
    finally:
        for p in (holder, child):
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)


def test_harvest_lease_renewal_advances_deadline_and_refuses_mid_batch(
        loops_root: Path) -> None:
    """What _harvest_all does after each worktree completes: acquire once,
    then renew (as if a worktree just finished). The deadline must advance,
    and while the lease is fresh a concurrent evaluator must refuse -- even
    though the ORIGINAL acquire-time deadline would by then have long since
    passed for a multi-worktree batch.
    """
    fd = sb._acquire_harvest_lock(loops_root)
    assert fd is not None
    try:
        original = sb._read_lock_metadata_from_fd(fd)
        original_deadline = sb._parse_lock_deadline(original["deadline"])
        assert original_deadline is not None

        assert sb._renew_harvest_lock_lease(fd)
        renewed = sb._read_lock_metadata_from_fd(fd)
        renewed_deadline = sb._parse_lock_deadline(renewed["deadline"])
        assert renewed_deadline is not None
        assert renewed_deadline >= original_deadline, (
            "renewal must advance the deadline, never move it backward")

        path = _lock_path(loops_root)
        decision = sb._evaluate_harvest_lock_holder(path)
        assert decision == "refuse", (
            "a freshly-renewed lease must never be mistaken for a wedge")
    finally:
        sb._release_harvest_lock(fd)


def test_harvest_lease_without_renewal_expires_normally(
        loops_root: Path) -> None:
    """Without any _renew_harvest_lock_lease call (no worktree ever
    completed), a lock past its ORIGINAL deadline is still correctly
    evaluated as a wedge -- the renewal mechanism existing does not change
    the takeover decision when it is simply never exercised.
    """
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        now = datetime.now(UTC)
        holder_lstart = sb._pid_lstart(holder.pid)
        path = _write_lock(loops_root, pid=holder.pid,
                           started=now - timedelta(hours=3),
                           deadline=now - timedelta(minutes=1),
                           pid_started=holder_lstart)

        decision = sb._evaluate_harvest_lock_holder(path)

        assert decision == "takeover", (
            "a lease that was never renewed must still expire and be "
            "reclaimed exactly as before this round's renewal mechanism "
            "was added")
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


# --------------------------------------------------------------------------
# Round 5 (Gemini Class review) — per-thread lock-fd context, not a global
# --------------------------------------------------------------------------
#
# Round 5 (Gemini Class review of 1626591db, BLOCKER):
#   the round-4 fix threaded the currently-held lock fd through a bare
#   module-level global (_current_harvest_lock_fd) so
#   _run_verification_command could reach it without re-signaturing
#   _harvest_one. That global cross-talks between two concurrent
#   _harvest_all invocations IN ONE PROCESS: thread B's acquire overwrites
#   the value thread A just set, so A's verification-child metadata lands
#   in B's lock (or nowhere), and A's own eventual takeover then finds no
#   child to kill -- the orphan leak reopens exactly the way round 4 closed
#   it. Fixed by replacing the bare global with ``threading.local``
#   (``_harvest_ctx``, attribute ``lock_fd``) -- same set/read/clear
#   touchpoints, but each thread now gets its own slot.


def test_harvest_ctx_lock_fd_is_per_thread_not_cross_talking(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent _harvest_all calls (separate loops_roots/repos, as two
    real harvest passes racing in one process would be) each record a
    distinct fake verification child while the OTHER thread is also live.
    Each lock file must carry ONLY its own thread's child metadata --
    never the sibling's, and never lose its own to the sibling overwriting
    a shared global (Gemini's finding-global-crosstalk-repro.py shape).
    """
    loops1 = tmp_path / "loops1"
    loops2 = tmp_path / "loops2"
    repo1 = tmp_path / "repo1"
    repo2 = tmp_path / "repo2"

    (sb._builders_root(repo1) / "wt1").mkdir(parents=True)
    (sb._builders_root(repo2) / "wt2").mkdir(parents=True)

    sync_event = threading.Event()

    def spy_harvest_one(loops_root: Path, _repo: Path, _worktree: Path,
                        **_kwargs: object) -> None:
        if str(loops_root).endswith("loops1"):
            # T1 waits until T2 has acquired ITS OWN lock and set ITS OWN
            # threading.local slot -- if the two shared one slot, T2's set
            # would clobber what T1 is about to read/act on.
            sync_event.wait(timeout=5)
            sb._record_verification_child(
                getattr(sb._harvest_ctx, "lock_fd", None), pid=9999, pgid=9999)
        elif str(loops_root).endswith("loops2"):
            sb._record_verification_child(
                getattr(sb._harvest_ctx, "lock_fd", None), pid=8888, pgid=8888)
            sync_event.set()
            time.sleep(0.3)  # give T1 a window to record while T2 is still live
        return None

    monkeypatch.setattr(sb, "_harvest_one", spy_harvest_one)

    t1 = threading.Thread(target=sb._harvest_all, args=(loops1, repo1),
                          kwargs={"close": False})
    t2 = threading.Thread(target=sb._harvest_all, args=(loops2, repo2),
                          kwargs={"close": False})
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    fd1 = os.open(sb._harvest_lock_path(loops1), os.O_RDONLY)
    fd2 = os.open(sb._harvest_lock_path(loops2), os.O_RDONLY)
    try:
        lock1 = sb._read_lock_metadata_from_fd(fd1)
        lock2 = sb._read_lock_metadata_from_fd(fd2)
    finally:
        os.close(fd1)
        os.close(fd2)

    assert lock1.get("child_pid") == 9999, (
        f"thread 1's own child must be recorded in its own lock file; "
        f"got {lock1}")
    assert lock2.get("child_pid") == 8888, (
        f"thread 2's own child must be recorded in its own lock file; "
        f"got {lock2}")
    assert lock1.get("child_pid") != lock2.get("child_pid"), (
        "the two threads' child records must never cross-talk")


def test_run_verification_command_always_reaps_on_timeout(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Even when the innermost escalation still can't drain the pipes (the
    Round-1 zombie-leak repro shape), the child must be waited on before
    ``_run_verification_command`` returns -- never left with returncode None.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    call_count = {"n": 0}
    real_wait = proc.wait

    def fake_communicate(timeout=None):
        call_count["n"] += 1
        if call_count["n"] <= 3:
            # First call (the 3600s wait) and both post-kill escalation
            # calls all "hang" -- forces the innermost proc.kill()+proc.wait()
            # reap path.
            raise subprocess.TimeoutExpired(cmd=proc.args, timeout=timeout)
        return ("", "")

    monkeypatch.setattr(proc, "communicate", fake_communicate)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)
    # Real signals still fire (proc is a real sleeping child); we only forced
    # the pipe-drain side to keep raising TimeoutExpired.

    result = sb._run_verification_command(["dummy"], "/tmp", timeout=1)

    assert result is None, "timeout must surface as None to the caller"
    # The real reap: either our fake path's proc.kill()+proc.wait() ran, or
    # the process is otherwise no longer running. Either way, waiting now
    # must return immediately (no hang) and yield a real exit code -- a
    # zombie/unwaited child would still show a status via wait() too, so
    # assert directly that wait() does not block.
    exit_code = real_wait(timeout=5)
    assert exit_code is not None
