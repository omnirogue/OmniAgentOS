"""The lease, proven with real OS processes. Threads would prove nothing here.

The thing under test is mutual exclusion *between interpreters*: a scheduler
firing the next tick while the last one is still running, an operator running a
loop by hand while cron runs it too. Every mechanism that provides it —
``fcntl.flock``, SQLite's write lock, the kernel dropping both when a process
dies — is arbitrated by the operating system and is invisible to a
``threading.Lock``. A threaded version of this file would exercise
:class:`~selfloop.lease.InProcessLease` semantics and pass while production
double-executed, which is exactly the shape of test this package exists to
refuse. So every process here is a real ``subprocess``.

Four claims, and the third and fourth are the counter-intuitive ones:

1. **Exactly one winner.** Six interpreters released simultaneously from a
   barrier; one acquires and five are refused, each naming what it could read
   about the holder. At the ``run_once`` level the loser reports ``IDLE`` — a
   non-result, out of the acceptance floor's numerator AND its denominator,
   because the tick it stood aside for is about to file the only honest account
   of that work. Counting a stand-aside against the floor is how a fleet
   auto-pauses itself on a busy morning.
2. **Release, don't leak.** After a clean exit and after ``SIGKILL``, the next
   process acquires immediately. That is free with ``flock`` — the kernel drops
   the lock however the holder exits — which is precisely what removes the need
   for a reclaim path, and removing the reclaim path is what removes the
   double-acquire race.
3. **A live holder is never stolen, and neither is an ancient one.** ``age_s``
   and ``stale_looking`` are computed and REPORTED, and nothing in any
   acquisition path reads them. The tests below construct a lease whose staleness
   threshold is 10 milliseconds, watch it label a live holder as stale-looking,
   and then watch it refuse to take the lease anyway. An age threshold that can
   *take* a lease is the unlink-and-recreate race in another costume: two
   processes both observe it as abandoned, both take it, both run.
4. **Releasing does not unlink the file.** Unlinking would let a peer blocked on
   this inode be joined by a later process that creates a DIFFERENT inode nobody's
   lock covers — two "leases" on one instance. An empty lease file costs one
   inode; the alternative costs correctness.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from selfloop.contracts import LeaseHeld
from selfloop.lease import (
    DEFAULT_MAX_AGE_S,
    FlockLease,
    InProcessLease,
    SqliteLease,
    flock_available,
    require_safe_lease_name,
)

PROTOTYPE_ROOT = Path(__file__).resolve().parents[1]

#: The instance every worker in this file contends for. Also a valid lease name,
#: which is checked by :func:`require_safe_lease_name` at acquisition time.
INSTANCE = "shared-instance"

#: How many interpreters pile onto the barrier. Six rather than two because a
#: two-process race can be won by luck; six failing to produce two winners is a
#: statement about arbitration rather than about scheduling.
RACERS = 6

needs_flock = pytest.mark.skipif(
    not flock_available(),
    reason="FlockLease requires POSIX fcntl.flock; SqliteLease is the portable alternative",
)


# ---------------------------------------------------------------------------
# Worker programs. Written to the test's tmp_path and run as real processes.
# ---------------------------------------------------------------------------

#: Contends for a lease and nothing else. Signals readiness, spins on a barrier
#: file so that every racer is past its imports before any of them attempts, then
#: makes exactly ONE attempt and records what happened.
#:
#: The readiness handshake is not ceremony. Without it a straggler could attempt
#: after the winner had already released, win legitimately, and produce a second
#: "won" — a flaky test that reads as a broken lease.
RACE_WORKER = '''\
import json
import os
import sys
import time
from pathlib import Path

from selfloop.contracts import LeaseHeld
from selfloop.lease import FlockLease, SqliteLease

workspace = Path(sys.argv[1])
backend = sys.argv[2]
index = sys.argv[3]
hold_s = float(sys.argv[4])

lease = (FlockLease if backend == "flock" else SqliteLease)(workspace / "leases")
result = workspace / ("racer-%s.json" % index)
(workspace / ("ready-%s" % index)).write_text("ready", encoding="utf-8")

barrier = workspace / "go"
deadline = time.monotonic() + 60.0
while not barrier.exists():
    if time.monotonic() > deadline:
        result.write_text(json.dumps({"outcome": "never_released"}), encoding="utf-8")
        raise SystemExit(1)
    time.sleep(0.005)

try:
    holding = lease.hold("shared-instance")
except LeaseHeld as refused:
    result.write_text(
        json.dumps(
            {
                "outcome": "lost",
                "pid": os.getpid(),
                "holder": dict(refused.holder),
                "detail": refused.detail,
            }
        ),
        encoding="utf-8",
    )
    raise SystemExit(0)

with holding:
    result.write_text(json.dumps({"outcome": "won", "pid": os.getpid()}), encoding="utf-8")
    time.sleep(hold_s)
'''

#: Holds the lease until told to stop, so the parent can interrogate it while it
#: is genuinely held by another OS process.
HOLD_WORKER = '''\
import json
import os
import sys
import time
from pathlib import Path

from selfloop.lease import FlockLease

workspace = Path(sys.argv[1])
max_s = float(sys.argv[2])

with FlockLease(workspace / "leases").hold("shared-instance"):
    (workspace / "held.json").write_text(
        json.dumps({"pid": os.getpid()}), encoding="utf-8"
    )
    stop = workspace / "stop"
    deadline = time.monotonic() + max_s
    while not stop.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
'''

#: A full ``run_once`` tick against durable storage. The "effect" is a line
#: appended to a shared file, which is the only ground truth a cross-process test
#: can have: it is written by the tool itself, outside anything this package
#: could be asked to vouch for.
TICK_WORKER = '''\
import json
import os
import sys
import time
from pathlib import Path

from selfloop.adapters.memory import ScriptedGate, build_memory_context, passing_receipt
from selfloop.adapters.sqlite import SqliteBackend
from selfloop.contracts import LoopTool, RiskTier
from selfloop.lease import FlockLease
from selfloop.runtime import run_once
from selfloop.templates.observe_decide_act_verify import NAME

workspace = Path(sys.argv[1])
role = sys.argv[2]
hold_s = float(sys.argv[3])

effects = workspace / "effects.log"


def observe(*, params):
    if role == "holder":
        # Written from INSIDE the lease, so the parent knows the exact moment a
        # peer is genuinely excluded rather than guessing with a sleep.
        (workspace / "held.json").write_text(
            json.dumps({"pid": os.getpid()}), encoding="utf-8"
        )
        time.sleep(hold_s)
    return [{"id": "subject-1"}]


def decide(*, subject):
    return {"action": "handle"}


def act(*, subject, decision):
    with effects.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "role": role}) + "\\n")
    return {"ok": True}


def verify(*, subject, decision, result):
    return {"verified": bool(result) and bool(result.get("ok"))}


backend = SqliteBackend(workspace / "loop.db")
ctx = build_memory_context(
    instance_id="shared-instance",
    template=NAME,
    lease=FlockLease(workspace / "leases"),
    gate=ScriptedGate(default=passing_receipt(detail="the effect line was written")),
    **backend.as_context_overrides(),
)
for name, tier, fn in (
    ("observe", RiskTier.T0, observe),
    ("decide", RiskTier.T0, decide),
    ("act", RiskTier.T1, act),
    ("verify", RiskTier.T0, verify),
):
    ctx.tools.register(LoopTool(name=name, tier=tier, call=fn))

report = run_once(ctx, NAME).as_dict()
report["pid"] = os.getpid()
(workspace / (role + ".report.json")).write_text(json.dumps(report), encoding="utf-8")
backend.close()
'''


# ---------------------------------------------------------------------------
# Process plumbing
# ---------------------------------------------------------------------------


def _env() -> dict[str, str]:
    """The child's environment: this checkout on the path, nothing else changed."""
    return {**os.environ, "PYTHONPATH": str(PROTOTYPE_ROOT)}


def _write_worker(workspace: Path, name: str, source: str) -> Path:
    """Materialise a worker program. A file, not ``-c``, so a traceback has lines."""
    path = workspace / name
    path.write_text(source, encoding="utf-8")
    return path


def _spawn(script: Path, *args: object) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell, test-owned paths
        [sys.executable, str(script), *[str(arg) for arg in args]],
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _finish(proc: subprocess.Popen[str], *, timeout: float = 90.0) -> str:
    """Wait for *proc* and insist it exited cleanly, surfacing its stderr if not."""
    out, err = proc.communicate(timeout=timeout)
    assert proc.returncode == 0, (
        f"worker exited {proc.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"
    )
    return err


def _await_file(path: Path, *, timeout: float = 60.0) -> dict[str, Any]:
    """Block until *path* holds parseable JSON, or fail with what is on disk.

    Polls rather than sleeps a fixed interval, because a fixed sleep is either
    slow or flaky and usually manages both.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass  # the writer is mid-write; look again
        time.sleep(0.01)
    raise AssertionError(f"{path} never appeared within {timeout}s")


@contextmanager
def _live_holder(workspace: Path, *, max_s: float = 30.0) -> Iterator[dict[str, Any]]:
    """Run a subprocess that holds the lease for the body of the ``with``."""
    script = _write_worker(workspace, "hold_worker.py", HOLD_WORKER)
    proc = _spawn(script, workspace, max_s)
    try:
        yield _await_file(workspace / "held.json")
    finally:
        (workspace / "stop").write_text("stop", encoding="utf-8")
        try:
            _finish(proc, timeout=30.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - the worker is wedged
            proc.kill()
            proc.communicate()
            raise


# ---------------------------------------------------------------------------
# 1. Exactly one winner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend",
    [pytest.param("flock", marks=needs_flock), "sqlite"],
)
def test_exactly_one_of_six_processes_acquires_the_lease(
    tmp_path: Path, backend: str
) -> None:
    """Six interpreters, one barrier, one winner. The kernel arbitrates.

    The predecessor reclaimed a lease it judged abandoned by ``unlink`` +
    re-``create``, which interleaves as ``A unlink -> A create -> B unlink
    (deleting A's fresh file) -> B create`` and hands the lease to BOTH. There is
    no such window here: exactly one ``LOCK_EX | LOCK_NB`` succeeds and the losers
    get ``EWOULDBLOCK``, and SQLite's ``BEGIN IMMEDIATE`` behaves the same way.
    """
    script = _write_worker(tmp_path, "race_worker.py", RACE_WORKER)
    procs = [_spawn(script, tmp_path, backend, index, 1.5) for index in range(RACERS)]

    for index in range(RACERS):
        deadline = time.monotonic() + 60.0
        while not (tmp_path / f"ready-{index}").exists():
            assert time.monotonic() < deadline, f"racer {index} never became ready"
            time.sleep(0.01)
    (tmp_path / "go").write_text("go", encoding="utf-8")

    for proc in procs:
        _finish(proc)

    results = [_await_file(tmp_path / f"racer-{index}.json") for index in range(RACERS)]
    winners = [row for row in results if row["outcome"] == "won"]
    losers = [row for row in results if row["outcome"] == "lost"]

    assert len(winners) == 1, f"expected one winner, got {results}"
    assert len(losers) == RACERS - 1, f"a racer neither won nor lost: {results}"
    assert len({row["pid"] for row in results}) == RACERS, "these were not real processes"


@needs_flock
def test_the_loser_is_told_who_holds_it_rather_than_being_left_to_guess(
    tmp_path: Path,
) -> None:
    """``LeaseHeld`` carries diagnostics so an operator can tell a peer from a mess.

    "A peer is working" and "something has been holding this for nine hours" need
    different responses, and the loser is the only process in a position to say
    which one it just saw.
    """
    with _live_holder(tmp_path) as holder:
        with pytest.raises(LeaseHeld) as refused:
            FlockLease(tmp_path / "leases").hold(INSTANCE)

    diagnostics = dict(refused.value.holder)
    assert diagnostics["pid"] == holder["pid"]
    assert diagnostics["backend"] == "flock"
    assert diagnostics["name"] == INSTANCE
    assert diagnostics["same_host"] is True
    assert str(holder["pid"]) in refused.value.detail


# ---------------------------------------------------------------------------
# 2. The loop level: one effect, and the loser reports a NON-RESULT
# ---------------------------------------------------------------------------


@needs_flock
def test_two_concurrent_ticks_produce_one_effect_and_a_neutral_stand_aside(
    tmp_path: Path,
) -> None:
    """The whole point of the lease, end to end, in two real processes.

    The contender is launched only once the holder has written its marker from
    *inside* the lease, so contention is a fact rather than a hope. What the
    contender must NOT do is as important as what it must: it must not run the
    tick, must not touch the world, and must not file a report card — its status
    is ``IDLE``, which the taxonomy classes as neutral and which is therefore
    absent from the acceptance floor's numerator and its denominator alike.
    """
    script = _write_worker(tmp_path, "tick_worker.py", TICK_WORKER)
    holder_proc = _spawn(script, tmp_path, "holder", 6.0)
    holder_marker = _await_file(tmp_path / "held.json")

    contender_proc = _spawn(script, tmp_path, "contender", 0.0)
    _finish(contender_proc, timeout=60.0)
    contender = _await_file(tmp_path / "contender.report.json")

    _finish(holder_proc, timeout=90.0)
    holder = _await_file(tmp_path / "holder.report.json")

    assert contender["status"] == "idle"
    assert contender["outcome"] == "neutral", "a stand-aside is a non-result, not a failure"
    assert contender["accepted"] is False, "and it is emphatically not an acceptance either"
    assert contender["effects"] == []
    assert "stood aside" in contender["detail"]
    assert str(holder_marker["pid"]) in contender["detail"]

    assert holder["status"] == "completed"
    assert holder["accepted"] is True
    assert holder["outcome"] == "favourable"

    lines = [
        json.loads(line)
        for line in (tmp_path / "effects.log").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 1, f"the effect happened {len(lines)} times: {lines}"
    assert lines[0]["role"] == "holder"
    assert lines[0]["pid"] == holder["pid"]


# ---------------------------------------------------------------------------
# 3. Released, not leaked — including after SIGKILL
# ---------------------------------------------------------------------------


@needs_flock
def test_the_lease_is_released_when_the_holder_exits_cleanly(tmp_path: Path) -> None:
    lease = FlockLease(tmp_path / "leases")
    with _live_holder(tmp_path):
        with pytest.raises(LeaseHeld):
            lease.hold(INSTANCE)

    # The holder has exited. No reclaim, no timeout, no sweeper: the descriptor
    # went with the process and the lock went with the descriptor.
    with lease.hold(INSTANCE):
        pass
    with lease.hold(INSTANCE):
        pass


@needs_flock
def test_sigkill_releases_the_lease_because_the_kernel_owns_it(tmp_path: Path) -> None:
    """Crash safety comes free, and that is what removes the reclaim path.

    A lease that needed a reclaim path would need a rule for when to take one,
    and every such rule is the double-acquire race. This test is the reason the
    package can refuse to have one.
    """
    script = _write_worker(tmp_path, "hold_worker.py", HOLD_WORKER)
    proc = _spawn(script, tmp_path, 60.0)
    _await_file(tmp_path / "held.json")

    lease = FlockLease(tmp_path / "leases")
    with pytest.raises(LeaseHeld):
        lease.hold(INSTANCE)

    proc.kill()  # SIGKILL: no finally block runs, nothing is unwound
    proc.communicate(timeout=30.0)
    assert proc.returncode != 0

    with lease.hold(INSTANCE):
        pass


# ---------------------------------------------------------------------------
# 4. A live holder is never stolen — however old it looks
# ---------------------------------------------------------------------------


@needs_flock
def test_a_live_holder_is_never_stolen_however_often_a_peer_asks(tmp_path: Path) -> None:
    lease = FlockLease(tmp_path / "leases")
    with _live_holder(tmp_path) as holder:
        for attempt in range(5):
            with pytest.raises(LeaseHeld) as refused:
                lease.hold(INSTANCE)
            assert refused.value.holder["pid"] == holder["pid"], f"attempt {attempt}"


@needs_flock
def test_an_ancient_lease_is_reported_stale_looking_but_never_taken(tmp_path: Path) -> None:
    """``stale_looking`` is a sentence for a human, not a permission for a process.

    The threshold here is ten milliseconds — far past absurd, and deliberately so:
    if any acquisition path anywhere read ``age_s`` or ``stale_looking``, this is
    the test that would take the lease. It does not, so the lease is refused with
    the staleness reported on the refusal.
    """
    stale_reader = FlockLease(tmp_path / "leases", max_age_s=0.01)
    with _live_holder(tmp_path) as holder:
        time.sleep(0.05)  # comfortably past the absurd threshold above

        with pytest.raises(LeaseHeld) as refused:
            stale_reader.hold(INSTANCE)
        diagnostics = dict(refused.value.holder)
        assert diagnostics["stale_looking"] is True, "the diagnostic must still be honest"
        assert diagnostics["age_s"] >= 0.0
        assert diagnostics["pid"] == holder["pid"]

        # And asking again, repeatedly, with the same absurd threshold, still fails.
        for _ in range(3):
            with pytest.raises(LeaseHeld):
                stale_reader.hold(INSTANCE)


def test_the_lease_exposes_no_reclaim_or_steal_operation() -> None:
    """The API surface itself refuses the idea. See the module docstring of lease.py."""
    for backend in (FlockLease, SqliteLease, InProcessLease):
        surface = [name for name in dir(backend) if not name.startswith("__")]
        forbidden = [
            name
            for name in surface
            if any(word in name.lower() for word in ("reclaim", "steal", "break", "force"))
        ]
        assert forbidden == [], f"{backend.__name__} exposes {forbidden}"
    assert DEFAULT_MAX_AGE_S > 0


# ---------------------------------------------------------------------------
# 5. Releasing does not unlink the file
# ---------------------------------------------------------------------------


@needs_flock
def test_releasing_does_not_unlink_the_lease_file(tmp_path: Path) -> None:
    """Same name, same inode, forever. Unlinking reintroduces the double-acquire.

    A peer blocked on this lock holds a descriptor onto THIS inode. Unlink it and
    a later ``open`` creates a different inode that nobody's lock covers, and two
    processes end up holding two "leases" on one instance.
    """
    lease = FlockLease(tmp_path / "leases")
    path = lease.path_for(INSTANCE)

    with lease.hold(INSTANCE):
        assert path.exists()
        inode = path.stat().st_ino

    assert path.exists(), "the lease file was unlinked on release"
    assert path.stat().st_ino == inode

    with lease.hold(INSTANCE):
        assert path.stat().st_ino == inode, "re-acquiring must not mint a new inode"


@needs_flock
def test_the_holder_record_is_one_fixed_size_atomic_write(tmp_path: Path) -> None:
    """512 bytes, never truncated, so a concurrent reader sees old OR new.

    A buffered write can be split into more than one syscall, and a reader that
    catches the gap sees an empty or unparseable file — from which it concludes
    "no holder" about a lease that is very much held. The lock never depended on
    this record; an operator's ability to answer "who has it?" does.
    """
    lease = FlockLease(tmp_path / "leases")
    path = lease.path_for(INSTANCE)

    with lease.hold(INSTANCE):
        raw = path.read_text(encoding="utf-8")
        assert path.stat().st_size == 512
        record = json.loads(raw)
        assert record["pid"] == os.getpid()
        assert record["backend"] == "flock"
        assert record["name"] == INSTANCE
        assert float(record["acquired_at"]) > 0

    # The record survives the release: the file is a note, and the note stays.
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert path.stat().st_size == 512


# ---------------------------------------------------------------------------
# 6. The lease that protects nothing must be chosen, never inherited
# ---------------------------------------------------------------------------


def test_the_in_process_lease_refuses_to_be_constructed_by_accident() -> None:
    """FIX-18: no silent degradation to a lock that excludes nothing.

    A quiet fallback to this on a host without ``fcntl`` is the worst available
    outcome — the loop keeps running, reports nothing unusual, and has stopped
    being safe. The argument exists so the choice appears in the caller's source
    where a reviewer can see it.
    """
    with pytest.raises(ValueError) as caught:
        InProcessLease()
    assert "protects nothing between OS processes" in str(caught.value)

    lease = InProcessLease(accept_single_process_only=True)
    with lease.hold(INSTANCE):
        with pytest.raises(LeaseHeld) as refused:
            lease.hold(INSTANCE)
        assert refused.value.holder["backend"] == "in_process"
        assert "another PROCESS" in refused.value.holder["note"]
    with lease.hold(INSTANCE):
        pass


@pytest.mark.parametrize(
    "unsafe", ["", "..", "../escape", "a/b", ".hidden", "name with spaces", "x" * 101]
)
def test_an_unsafe_lease_name_is_refused_rather_than_sanitised(unsafe: str) -> None:
    """Sanitising can map two instances onto one file, which is worse than no lease.

    Two unrelated loops serialised against each other, each with a false sense of
    exclusion, and nothing anywhere saying so.
    """
    with pytest.raises(ValueError, match="unsafe lease name"):
        require_safe_lease_name(unsafe)


def test_a_safe_lease_name_survives_intact() -> None:
    for name in ("a", "loop-1", "daily.digest_v2", INSTANCE):
        assert require_safe_lease_name(name) == name
