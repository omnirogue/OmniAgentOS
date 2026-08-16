"""Two real workers, one loop instance: exactly one may advance it.

Without a lease, both processes read the same checkpoint snapshot, both start a
tick, and both reach the effect. The receipt's check-then-claim then decides the
race: one executes, the loser raises ``EffectStateUnknown`` and reports a
spurious ABORT — which counts against the routine acceptance floor and can pause
a healthy loop. So the test asserts BOTH halves: the external ledger holds one
line, and the loser reports a non-adverse status rather than a failure.

The processes are real (``subprocess``, separate interpreters). A threading test
would prove nothing here: the thing under test is a filesystem lease between OS
processes, which is exactly what launchd overlap produces.

Two shapes of evidence, because a race needs both:

* **deterministic** — a live holder is never displaced, no matter how abandoned
  its file *looks* (``test_an_ancient_lease_is_reported_but_never_stolen``,
  ``test_a_live_holder_is_not_stolen``). These are the ones that fail outright
  against the previous unlink/re-create reclaim scheme, which handed the lease
  to a second caller in exactly that state.
* **contended** — N processes released together by a barrier file against a
  stale-looking lease, repeated
  (``test_simultaneous_workers_against_a_stale_looking_lease_leave_exactly_one_winner``).
  This is the shape production actually produces, and it is deliberately not
  the only evidence: a timing race can hide for many rounds.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from omniagentos_loops import lease
from omniagentos_loops.contracts import LoopStatus

DRILL = Path(__file__).resolve().parent / "drills" / "kill_drill.py"
HOLDER = Path(__file__).resolve().parent / "drills" / "lease_holder.py"
LOOPS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = LOOPS_DIR.parent
TEMPLATE = "monitor_diagnose_repair_verify"  # T1 auto-remedy: no human in the way
INSTANCE = "concurrency_probe"

#: Racers per round of the simultaneous-acquire test, and rounds. Three racers
#: (not two) because the broken predecessor could also produce a THREE-way
#: acquire, and one round proves nothing about a race — it has to lose
#: repeatedly to be evidence.
RACERS = 3
ROUNDS = 6


def _spawn_holder(
    barrier: Path, *, hold_s: float = 0.0, ready: Path | None = None
) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{REPO_ROOT}:{LOOPS_DIR}"
    argv = [
        sys.executable,
        str(HOLDER),
        "--barrier",
        str(barrier),
        "--template",
        TEMPLATE,
        "--instance",
        INSTANCE,
        "--hold",
        str(hold_s),
    ]
    if ready is not None:
        argv += ["--ready", str(ready)]
    return subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
    )


def _verdict(proc: subprocess.Popen) -> dict:
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, err
    return json.loads(out.strip().splitlines()[-1])


def _seed_stale_looking_lease(path: Path) -> None:
    """A lease file whose CONTENTS beg to be reclaimed.

    Dead PID, ancient timestamp: under the previous unlink/re-create scheme this
    is exactly the state in which every racer independently judged the lease
    abandoned and started deleting it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": 999_000_001, "acquired_at": time.time() - 10_000}), encoding="utf-8"
    )


def _spawn(db: str, ledger: Path, *, hold_s: str = "0") -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{REPO_ROOT}:{LOOPS_DIR}"
    env["KILL_DRILL_EFFECT_DELAY_S"] = hold_s
    return subprocess.Popen(
        [
            sys.executable,
            str(DRILL),
            "--db",
            db,
            "--ledger",
            str(ledger),
            "--template",
            TEMPLATE,
            "--instance",
            INSTANCE,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _report(proc: subprocess.Popen) -> dict:
    out, err = proc.communicate(timeout=180)
    assert proc.returncode == 0, err
    for line in reversed(out.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "status" in parsed:
            return parsed
    raise AssertionError(f"no report: {out!r} {err!r}")


def _lines(ledger: Path) -> list[str]:
    return ledger.read_text().splitlines() if ledger.exists() else []


def test_two_concurrent_workers_produce_exactly_one_effect(db_path, loops_root, tmp_path):
    ledger = tmp_path / "external_ledger.log"

    # The first worker holds its effect open long enough for the second to start
    # and find the lease taken — the launchd-overlap shape.
    first = _spawn(db_path, ledger, hold_s="2.5")
    time.sleep(0.6)
    second = _spawn(db_path, ledger)

    first_report = _report(first)
    second_report = _report(second)

    assert len(_lines(ledger)) == 1, (
        f"the effect ran more than once: {_lines(ledger)} "
        f"({first_report['status']} / {second_report['status']})"
    )
    statuses = {first_report["status"], second_report["status"]}
    assert LoopStatus.COMPLETED.value in statuses
    assert LoopStatus.IDLE.value in statuses, "the loser must step aside, not fail"

    loser = second_report if second_report["status"] == LoopStatus.IDLE.value else first_report
    assert loser["outcome"] == "neutral", (
        "a tick that stepped aside is a NON-RESULT; scoring it unfavourable "
        "would pause a healthy loop the moment two ticks overlap, and scoring "
        "it favourable would let two loops racing each other look productive"
    )
    assert loser["accepted"] is False, "stepping aside is not an acceptance"
    assert "another worker holds this instance" in loser["detail"]


def test_a_sequential_second_tick_is_not_blocked(db_path, loops_root, tmp_path):
    """The lease must be released, not leaked, when a tick finishes."""
    ledger = tmp_path / "external_ledger.log"
    assert _report(_spawn(db_path, ledger))["status"] == LoopStatus.COMPLETED.value
    assert _report(_spawn(db_path, ledger))["status"] == LoopStatus.COMPLETED.value
    assert len(_lines(ledger)) == 1, "second tick replays the receipt, not the effect"


def test_simultaneous_workers_against_a_stale_looking_lease_leave_exactly_one_winner(
    loops_root, tmp_path
):
    """THE lease property: N racers, one winner. Every round.

    The predecessor scheme reclaimed a lease it judged abandoned by ``unlink``
    then ``O_EXCL create``. Two racers that both read the same stale holder could
    interleave as ``A unlink -> A create -> B unlink (removes A's fresh file) ->
    B create`` and BOTH return holding it — mutual exclusion silently gone in
    exactly the state the reclaim path exists for. So the racers here start from
    a lease file that LOOKS abandoned (dead PID, ancient timestamp) and are
    released together by a barrier file, which is the only way to get them into
    the same microsecond.

    ``fcntl.flock`` has no such window: the kernel decides, once, for everyone.
    """
    path = lease.lease_path(TEMPLATE, INSTANCE)
    for round_index in range(ROUNDS):
        _seed_stale_looking_lease(path)
        barrier = tmp_path / f"go-{round_index}"
        procs = [_spawn_holder(barrier, hold_s=0.25) for _ in range(RACERS)]
        time.sleep(0.35)  # every racer is spinning on the barrier before it drops
        barrier.write_text("go", encoding="utf-8")
        verdicts = [_verdict(proc) for proc in procs]

        winners = [v for v in verdicts if v["acquired"]]
        assert len(winners) == 1, (
            f"round {round_index}: {len(winners)} of {RACERS} racers acquired the same "
            f"instance lease — mutual exclusion is broken ({verdicts})"
        )
        for loser in (v for v in verdicts if not v["acquired"]):
            assert loser["holder"], "a loser must report WHO holds it, for the operator"


def test_a_live_holder_is_not_stolen(loops_root):
    held = lease.acquire(TEMPLATE, INSTANCE)
    try:
        with pytest.raises(lease.LeaseHeld):
            lease.acquire(TEMPLATE, INSTANCE)
    finally:
        lease.release(held)
    # Released: the next acquire succeeds.
    lease.release(lease.acquire(TEMPLATE, INSTANCE))


def test_a_killed_holder_releases_the_lease(loops_root, tmp_path):
    """``kill -9`` must not wedge the instance — and nothing may reclaim before it.

    This is the property the deleted PID-liveness probe was TRYING to provide,
    obtained instead from the kernel: a flock dies with the file descriptor that
    holds it, however the process ends. The negative half is asserted first, and
    it is the important half — while the holder is alive the lease is refused,
    so there is no window in which a second worker "helpfully" reclaims a lease
    that is still in use.
    """
    barrier = tmp_path / "go"
    ready = tmp_path / "ready"
    holder = _spawn_holder(barrier, hold_s=120.0, ready=ready)
    try:
        barrier.write_text("go", encoding="utf-8")
        deadline = time.time() + 60.0
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "the holder never took the lease"

        with pytest.raises(lease.LeaseHeld):
            lease.acquire(TEMPLATE, INSTANCE)

        holder.kill()
        holder.wait(timeout=30)
    finally:
        if holder.poll() is None:  # pragma: no cover - only on an assertion failure
            holder.kill()
            holder.wait(timeout=30)

    reclaimed = lease.acquire(TEMPLATE, INSTANCE)
    assert reclaimed.pid == os.getpid()
    lease.release(reclaimed)


def test_an_ancient_lease_is_reported_but_never_stolen(loops_root):
    """Age is DIAGNOSTIC. A threshold that can take a lease is the race again.

    The previous revision reclaimed any lease older than ``max_age_s`` even with
    a live holder, to defend against PID reuse — but under flock there is no PID
    to reuse and no reclaim to race, and a live holder is by definition a tick
    still running. So an old lease is flagged for a human and left alone.
    """
    held = lease.acquire(TEMPLATE, INSTANCE)
    held.path.write_text(
        json.dumps({"pid": os.getpid(), "acquired_at": time.time() - 10_000}), encoding="utf-8"
    )
    try:
        with pytest.raises(lease.LeaseHeld) as caught:
            lease.acquire(TEMPLATE, INSTANCE, max_age_s=3600)
    finally:
        lease.release(held)
    assert caught.value.holder["stale_looking"] is True
    assert caught.value.holder["age_s"] >= 10_000


def test_the_lease_file_survives_release(loops_root):
    """A stable inode is what flock arbitrates over; unlinking would split it."""
    held = lease.acquire(TEMPLATE, INSTANCE)
    inode = held.path.stat().st_ino
    lease.release(held)
    assert held.path.exists(), "releasing must not unlink the lease file"
    again = lease.acquire(TEMPLATE, INSTANCE)
    assert again.path.stat().st_ino == inode
    lease.release(again)


def test_leases_are_per_instance_not_global(loops_root):
    a = lease.acquire(TEMPLATE, "instance_a")
    b = lease.acquire(TEMPLATE, "instance_b")
    assert a.path != b.path
    lease.release(a)
    lease.release(b)
