"""The session lane's scope-lock wiring, proved at the supervisor boundary.

``tests/scope/test_locks.py`` proves the STORE (atomicity, fencing, FIFO). This
file proves the LANE: that the supervisor declares the right thing, claims it
before the process exists, gives it back on every terminal path, and — the
acceptance criterion that outranks all the others — does absolutely nothing at
all while the feature is off.

The off-mode test is deliberately not "assert the tables are empty". Empty tables
would also be produced by a wiring that runs every read, shells out to ``git``
for a realm and then declines to write; that is not "byte-for-byte unchanged",
it is a silent latency regression on the hottest loop in the system. So the test
makes the lock store itself EXPLODE if it is constructed, which catches the reads
and the realm resolution as well as the writes.

Most of these tests need a child process that is still RUNNING when the assertion
happens — a lock released correctly by ``_finish`` is indistinguishable from a
lock never taken if the fake process has already exited by the time the test
looks. Hence :class:`HeldProcess`, which stays alive until the test says
otherwise.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from omniagentos.scope import paths as scope_paths
from omniagentos.scope.locks import LockHolder, PathLockStore
from omniagentos.scope.model import ScopeClaim
from omniagentos.sessions import hook_token, scope_wiring, ssh_keys
from omniagentos.sessions import supervisor as supervisor_module
from omniagentos.sessions.dal import SessionsDal, SessionState
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.sessions.scope_wiring import (
    LANE_OWNED_TITLE_MARKERS,
    ScopeDecision,
    SessionScopeGate,
    claims_for_roots,
    decode_scope,
    encode_scope,
    is_lane_owned,
    session_holder,
)
from omniagentos.sessions.supervisor import SessionSupervisor

# ---------------------------------------------------------------------------
# Fixtures / harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _sandbox_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the wiring tests independent of the host OS sandbox."""
    monkeypatch.setattr("omniagentos.runner.sandbox.sandbox_available", lambda: True)
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.sandbox.wrap_command",
        lambda argv, _cwd, **_kwargs: list(argv),
    )
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )


@pytest.fixture(autouse=True)
def _credentials_isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Per-session hook/ssh credential files stay inside the test's tmp dir."""
    monkeypatch.setattr(hook_token, "HOOK_TOKENS_ROOT", tmp_path / "hook-tokens")
    monkeypatch.setattr(ssh_keys, "SSH_KEYS_ROOT", tmp_path / "ssh-keys")


@pytest.fixture(autouse=True)
def _no_git_realm_shellout(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Realms come from realpath alone, so a tmp dir is its own realm.

    Without this the test process would shell out to ``git rev-parse`` per
    directory, and — worse — a ``tmp_path`` that happened to sit inside a
    checkout would fold into that repo's realm and make two unrelated tests
    conflict with each other. The stub is ``lru_cache``-wrapped so it still
    satisfies ``clear_realm_cache``, which runs while the patch is live.
    """
    scope_paths.clear_realm_cache()
    monkeypatch.setattr(
        scope_paths, "_git_toplevel", functools.lru_cache(maxsize=1)(lambda _start: None)
    )
    yield
    scope_paths.clear_realm_cache()


@pytest.fixture
def sessions_dal(tmp_path: Path) -> Iterator[SessionsDal]:
    """The DAL, closed only once every reader thread has finished with it.

    ``_read_process`` runs on a daemon thread that outlives the test body, and it
    ends in ``_finish`` -> ``_ensure_manifest``, both of which read the database.
    Closing underneath it raises on that thread, which pytest surfaces as an
    unrelated warning on whichever test happens to be running at the time.
    """
    dal = SessionsDal(tmp_path / "sessions.db")
    yield dal
    for thread in threading.enumerate():
        if thread.name.startswith("session-"):
            thread.join(timeout=3.0)
    dal.close()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    return directory


class FakeProcess:
    """A child that has already exited by the time the reader thread looks."""

    next_pid = 90000

    def __init__(self, lines: list[dict[str, Any]] | None = None, returncode: int = 0) -> None:
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.stdout = [json.dumps(line) + "\n" for line in (lines or [])]
        self.returncode: int | None = returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return int(self.returncode or 0)

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class HeldProcess(FakeProcess):
    """A child that stays alive until the test releases it.

    ``stdout = None`` makes ``_read_process`` skip straight to ``wait()``, which
    blocks here — so the session sits in ``running`` with its locks held while
    the test asserts, exactly as a real one would.
    """

    def __init__(self) -> None:
        super().__init__(returncode=0)
        self.stdout = None
        self.returncode = None
        self._exited = threading.Event()

    def wait(self, timeout: float | None = None) -> int:
        self._exited.wait(5.0 if timeout is None else timeout)
        return int(self.returncode or 0)

    def poll(self) -> int | None:
        return self.returncode

    def finish(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self._exited.set()

    def terminate(self) -> None:
        self.finish(-15)

    def kill(self) -> None:
        self.finish(-9)


@pytest.fixture
def held(sessions_dal: SessionsDal) -> Iterator[list[HeldProcess]]:
    """Every :class:`HeldProcess` a test launched, released at teardown.

    Depends on ``sessions_dal`` so it finalizes FIRST — the processes must be let
    go before that fixture waits for the reader threads, or the wait is for a
    thread that is still blocked on a process nothing will ever end.
    """
    made: list[HeldProcess] = []
    yield made
    for process in made:
        process.finish(0)


def held_builder(made: list[HeldProcess]) -> Callable[[], HeldProcess]:
    def build() -> HeldProcess:
        process = HeldProcess()
        made.append(process)
        return process

    return build


def recording_factory(
    launches: list[list[str]],
    build: Callable[[], FakeProcess],
) -> Callable[..., FakeProcess]:
    def factory(argv: list[str], **_kwargs: Any) -> FakeProcess:
        launches.append(list(argv))
        return build()

    return factory


def make_supervisor(
    dal: SessionsDal,
    tmp_path: Path,
    launches: list[list[str]],
    *,
    build: Callable[[], FakeProcess] = FakeProcess,
) -> SessionSupervisor:
    supervisor = SessionSupervisor(
        dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=recording_factory(launches, build),
        notifier=lambda _title, _body: None,
    )
    # The gate caches the resolved mode for a second so the poll loop does not
    # re-parse parallelism.yaml per session; tests flip the env mid-run, so they
    # opt out of the cache entirely rather than sleeping.
    supervisor._scope = SessionScopeGate(dal, mode_cache_s=0.0)
    return supervisor


def set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SCOPE_LOCKS", mode)


def live_locks(dal: SessionsDal) -> list[sqlite3.Row]:
    return dal._connection.execute(
        "SELECT * FROM resource_locks WHERE released_at IS NULL"
    ).fetchall()


def live_waiters(dal: SessionsDal) -> list[sqlite3.Row]:
    return dal._connection.execute(
        "SELECT * FROM scope_waiters WHERE cleared_at IS NULL"
    ).fetchall()


def queued_requests(dal: SessionsDal) -> list[sqlite3.Row]:
    return dal._connection.execute(
        "SELECT * FROM session_spawn_queue WHERE state = 'queued'"
    ).fetchall()


def wait_for_state(dal: SessionsDal, session_id: str, state: str) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        row = dal.get_session(session_id)
        if row is not None and row["state"] == state:
            return
        time.sleep(0.01)
    row = dal.get_session(session_id)
    raise AssertionError(
        f"session {session_id} did not reach {state} (is {row['state'] if row else 'missing'})"
    )


def wait_until(predicate: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def hold_foreign_scope(dal: SessionsDal, path: Path, holder_id: str = "run_other") -> str:
    """Take a whole-subtree lock on ``path`` as a DIFFERENT lane, and return its id."""
    store = PathLockStore(dal)
    claims = claims_for_roots([str(path)])
    assert claims, "the test's own foreign claim must be resolvable"
    result = store.try_acquire_scope(
        claims, LockHolder(kind="run", id=holder_id, lane="runner"), enforce=True
    )
    assert result.status == "granted", result
    return holder_id


# ---------------------------------------------------------------------------
# (a) OFF is byte-for-byte unchanged — the acceptance criterion
# ---------------------------------------------------------------------------


def test_default_mode_is_shadow_via_shipped_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code default is still ``off``; Grok's shipped config elevates to shadow.

    Without clearing the env, ``scope_locks_mode()`` must resolve to the
    configs/parallelism.yaml value (``shadow``) so soak rows are written but
    conflicts are never refused. Tests that need true ``off`` call ``set_mode``.
    """
    from omniagentos.scope.config import DEFAULT_SCOPE_MODE, scope_locks_mode

    monkeypatch.delenv("OMNIAGENTOS_SCOPE_LOCKS", raising=False)
    assert DEFAULT_SCOPE_MODE == "off"
    assert scope_locks_mode() == "shadow"


def test_off_mode_never_touches_the_lock_store(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OFF must be byte-for-byte today's behaviour: no rows, and no READS either.

    Constructing a PathLockStore is made fatal, which is strictly stronger than
    asserting the tables are empty — it also catches a wiring that resolves
    realms, shells out to git or issues SELECTs before deciding not to write.
    """
    set_mode(monkeypatch, "off")

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the lock store must never be built while mode is off")

    monkeypatch.setattr(scope_wiring, "PathLockStore", explode)

    launches: list[list[str]] = []
    supervisor = make_supervisor(sessions_dal, tmp_path, launches)
    granted = tmp_path / "granted"
    granted.mkdir()

    session_id = supervisor.spawn(
        str(project),
        "haiku",
        "do work",
        granted_roots=[str(granted)],
    )
    wait_for_state(sessions_dal, session_id, "completed")

    # The launch happened exactly as before, and every lifecycle path that now
    # carries a scope call is exercised.
    assert len(launches) == 1
    supervisor._process_spawn_queue()
    supervisor.reconcile()
    supervisor._service_session(session_id, sessions_dal.get_session(session_id) or {})
    supervisor._kill_session(session_id)

    assert live_locks(sessions_dal) == []
    assert live_waiters(sessions_dal) == []
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["scope_json"] is None
    assert row["state"] == "completed"
    # Explicit grant preserved; standing roots (AUTO-APPROVE Phase 1) may also appear.
    assert str(granted) in json.loads(row["granted_roots"])


def test_off_mode_gate_methods_are_inert(
    sessions_dal: SessionsDal,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every gate entry point short-circuits, so no caller can force a write."""
    set_mode(monkeypatch, "off")
    gate = SessionScopeGate(sessions_dal, mode_cache_s=0.0)
    assert gate.mode == "off"
    assert gate.armed is False
    assert gate.open_for_launch("ses_x", project_dir=str(project)).outcome == "off"
    assert gate.open_for_row({"id": "ses_x", "project_dir": str(project)}).outcome == "off"
    assert gate.adopt({"id": "ses_x", "project_dir": str(project)}).outcome == "off"
    assert gate.renew({"id": "ses_x"}) is False
    assert gate.release("ses_x") == 0
    assert live_locks(sessions_dal) == []


# ---------------------------------------------------------------------------
# (b) enforce refuses a conflicting claim; the lane parks instead of executing
# ---------------------------------------------------------------------------


def test_enforce_refuses_conflicting_spawn_and_parks(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked session does NOT launch, does NOT raise, and DOES park durably."""
    set_mode(monkeypatch, "enforce")
    hold_foreign_scope(sessions_dal, project)

    launches: list[list[str]] = []
    supervisor = make_supervisor(sessions_dal, tmp_path, launches)

    session_id = supervisor.spawn(str(project), "haiku", "do work")

    # No process. spawn's contract still held: a usable durable id came back.
    assert launches == []
    assert session_id.startswith("ses_")
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == SessionState.STARTING.value
    assert row["pid"] is None
    # The declaration was still recorded even though the claim was refused.
    assert row["scope_json"] is not None

    waiters = live_waiters(sessions_dal)
    assert [w["holder_id"] for w in waiters] == [session_id]
    assert waiters[0]["holder_kind"] == "session"
    assert waiters[0]["lane"] == "session"
    assert waiters[0]["blocked_on"]  # the observed blocker's lock id (diagnostic)

    # It took NO locks of its own -- all-or-nothing, so the only live rows are
    # the foreign holder's.
    assert {lock["holder_id"] for lock in live_locks(sessions_dal)} == {"run_other"}


def test_blocked_spawn_is_retried_by_the_durable_ingress(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    held: list[HeldProcess],
) -> None:
    """The park reuses the spawn queue: still blocked -> still queued, then launches.

    The request must stay ``queued`` across a refused pass. If the ingress claimed
    it first and only then discovered the refusal, the request would be burnt and
    the session stranded in ``starting`` with nothing left to launch it.
    """
    set_mode(monkeypatch, "enforce")
    hold_foreign_scope(sessions_dal, project)

    launches: list[list[str]] = []
    supervisor = make_supervisor(sessions_dal, tmp_path, launches, build=held_builder(held))
    session_id = supervisor.spawn(str(project), "haiku", "do work")

    assert [r["session_id"] for r in queued_requests(sessions_dal)] == [session_id]

    # Pass 1: still blocked. Nothing launches and the request survives.
    supervisor._process_spawn_queue()
    assert launches == []
    assert [r["session_id"] for r in queued_requests(sessions_dal)] == [session_id]

    # The blocker finishes.
    PathLockStore(sessions_dal).release_scope("run", "run_other", 0)

    # Pass 2: granted, launched, waiter cleared, locks now held by the session.
    supervisor._process_spawn_queue()
    assert len(launches) == 1
    assert queued_requests(sessions_dal) == []
    assert live_waiters(sessions_dal) == []
    assert {lock["holder_id"] for lock in live_locks(sessions_dal)} == {session_id}


def test_lane_owned_sessions_take_no_locks_of_their_own(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swarm/longhaul session launches THROUGH a conflict rather than into it.

    Its coordinator already holds the scope. If the session claimed the same
    paths under its own holder identity it would be refused by its own owner and
    park forever, which is a self-deadlock, not contention.
    """
    set_mode(monkeypatch, "enforce")
    hold_foreign_scope(sessions_dal, project, holder_id="run_coordinator")

    launches: list[list[str]] = []
    supervisor = make_supervisor(sessions_dal, tmp_path, launches)

    session_id = supervisor.spawn(
        str(project), "haiku", "do work", title="build", title_prefix="[swarm:att_1]"
    )
    wait_for_state(sessions_dal, session_id, "completed")

    assert len(launches) == 1
    assert live_waiters(sessions_dal) == []
    assert {lock["holder_id"] for lock in live_locks(sessions_dal)} == {"run_coordinator"}
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["scope_json"] is None  # exempt sessions declare nothing


def test_exemption_markers_match_the_supervisors() -> None:
    """The scope exemption and the respawn-ownership check must agree, forever."""
    assert set(LANE_OWNED_TITLE_MARKERS) == {
        supervisor_module._LONGHAUL_TITLE_MARKER,
        supervisor_module._SWARM_TITLE_MARKER,
    }
    for marker in LANE_OWNED_TITLE_MARKERS:
        title = f"{marker}att_9] build the thing"
        assert is_lane_owned(title) is True
        assert supervisor_module._scheduler_owns_respawn(title) is True
    assert is_lane_owned("ordinary title") is False
    assert is_lane_owned(None) is False


# ---------------------------------------------------------------------------
# (c) release on every terminal path, including a crash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("returncode", "expected_state"),
    [
        (0, SessionState.COMPLETED.value),
        (1, SessionState.FAILED.value),  # crash: non-zero exit, no result event
    ],
)
def test_scope_released_on_process_exit(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    held: list[HeldProcess],
    returncode: int,
    expected_state: str,
) -> None:
    """Clean exit and crash both funnel through _finish, so both give the paths back."""
    set_mode(monkeypatch, "enforce")
    launches: list[list[str]] = []
    supervisor = make_supervisor(sessions_dal, tmp_path, launches, build=held_builder(held))

    session_id = supervisor.spawn(str(project), "haiku", "do work")
    # Held: the process is still running, so this is a real "locks are held while
    # the session works" assertion rather than a race with its own teardown.
    assert {lock["holder_id"] for lock in live_locks(sessions_dal)} == {session_id}

    held[0].finish(returncode)
    wait_for_state(sessions_dal, session_id, expected_state)
    wait_until(lambda: live_locks(sessions_dal) == [], "the scope to be released")
    assert live_waiters(sessions_dal) == []


def test_scope_released_on_kill(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    held: list[HeldProcess],
) -> None:
    """The kill path releases AFTER the process is dead — never in the reaper."""
    set_mode(monkeypatch, "enforce")
    launches: list[list[str]] = []
    supervisor = make_supervisor(sessions_dal, tmp_path, launches, build=held_builder(held))

    session_id = supervisor.spawn(str(project), "haiku", "do work")
    assert {lock["holder_id"] for lock in live_locks(sessions_dal)} == {session_id}

    sessions_dal.request_kill(session_id, killed_by="idle-reaper")
    supervisor._kill_session(session_id)

    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == SessionState.KILLED.value
    assert row["killed_by"] == "idle-reaper"
    assert live_locks(sessions_dal) == []


def test_reaper_does_not_release_while_the_process_is_alive(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    held: list[HeldProcess],
) -> None:
    """The reaper only REQUESTS a kill; releasing there would hand a live writer's
    paths to a second writer, which is the exact bug the locks exist to prevent."""
    set_mode(monkeypatch, "enforce")
    monkeypatch.setenv("OMNIAGENTOS_REAPER_ENFORCE", "1")
    launches: list[list[str]] = []
    supervisor = make_supervisor(sessions_dal, tmp_path, launches, build=held_builder(held))

    session_id = supervisor.spawn(str(project), "haiku", "do work")
    assert {lock["holder_id"] for lock in live_locks(sessions_dal)} == {session_id}

    supervisor._reap(
        session_id,
        reason="idle",
        killed_by="idle-reaper",
        idle_seconds=999.0,
        threshold_seconds=1.0,
        detail="test",
    )
    # kill_requested is set, the process has NOT been signalled yet, the lock stays.
    row = sessions_dal.get_session(session_id)
    assert row is not None and bool(row["kill_requested"])
    assert {lock["holder_id"] for lock in live_locks(sessions_dal)} == {session_id}

    # And the kill that follows on the next poll DOES release it.
    supervisor._kill_session(session_id)
    assert live_locks(sessions_dal) == []


def test_terminal_poll_releases_a_row_finished_elsewhere(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    held: list[HeldProcess],
) -> None:
    """A row terminalized outside _finish (API cancel, reconcile) is still released."""
    set_mode(monkeypatch, "enforce")
    launches: list[list[str]] = []
    supervisor = make_supervisor(sessions_dal, tmp_path, launches, build=held_builder(held))
    session_id = supervisor.spawn(str(project), "haiku", "do work")
    assert live_locks(sessions_dal)

    sessions_dal.terminalize_session(
        session_id, SessionState.CANCELLED.value, void_note="cancelled elsewhere"
    )
    supervisor._service_session(session_id, sessions_dal.get_session(session_id) or {})

    assert live_locks(sessions_dal) == []


# ---------------------------------------------------------------------------
# (d) the fence rejects a stale generation
# ---------------------------------------------------------------------------


def test_fenced_generation_refuses_the_launch(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session identity that was adopted away at a higher generation must STOP.

    Not park: a retry at the same generation is refused again, and a retry at a
    bumped one would be the displaced worker stealing its adopter's locks back.
    So the request fails and the session terminalizes rather than looping.
    """
    set_mode(monkeypatch, "enforce")
    session_id = "ses_fenced_0001"
    sessions_dal.create_session(
        {
            "id": session_id,
            "source": "bridge",
            "project_dir": str(project),
            "provider": "claude",
            "session_ref": "11111111-2222-3333-4444-555555555555",
            "state": SessionState.STARTING.value,
            "model": "haiku",
        }
    )
    # Somebody adopted this identity and took its scope at generation 5.
    store = PathLockStore(sessions_dal)
    adopted = store.try_acquire_scope(
        claims_for_roots([str(project)]),
        session_holder(session_id),
        generation=5,
        enforce=True,
    )
    assert adopted.status == "granted"

    sessions_dal.enqueue_spawn(
        session_id=session_id,
        project_dir=str(project),
        model="haiku",
        prompt="do work",
        budget_usd_max=None,
        title=None,
    )
    launches: list[list[str]] = []
    supervisor = make_supervisor(sessions_dal, tmp_path, launches)
    supervisor._process_spawn_queue()

    assert launches == []
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["state"] == SessionState.FAILED.value
    # Fenced is not parked: no waiter row, and no retry left in the queue.
    assert live_waiters(sessions_dal) == []
    assert queued_requests(sessions_dal) == []
    # The adopter's generation-5 locks are untouched.
    assert {lock["holder_generation"] for lock in live_locks(sessions_dal)} == {5}


def test_gate_reports_fenced_without_writing(
    sessions_dal: SessionsDal, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate surfaces the fence as its own outcome, distinct from a park."""
    set_mode(monkeypatch, "enforce")
    gate = SessionScopeGate(sessions_dal, mode_cache_s=0.0)
    store = PathLockStore(sessions_dal)
    store.try_acquire_scope(
        claims_for_roots([str(project)]),
        session_holder("ses_gen"),
        generation=3,
        enforce=True,
    )
    decision = gate.open_for_launch("ses_gen", project_dir=str(project))
    assert decision.outcome == "fenced"
    assert decision.fenced is True
    assert decision.may_launch is False
    assert live_waiters(sessions_dal) == []


# ---------------------------------------------------------------------------
# Declaration: derived from the grants, so the two cannot desync
# ---------------------------------------------------------------------------


def test_declared_scope_is_derived_from_the_frozen_grants(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scope_json covers exactly project_dir + granted_roots: one input, no desync."""
    set_mode(monkeypatch, "shadow")
    granted = tmp_path / "granted"
    granted.mkdir()
    launches: list[list[str]] = []
    supervisor = make_supervisor(sessions_dal, tmp_path, launches)

    session_id = supervisor.spawn(str(project), "haiku", "do work", granted_roots=[str(granted)])
    row = sessions_dal.get_session(session_id)
    assert row is not None

    declared = decode_scope(row["scope_json"])
    realms = {claim.realm for claim in declared}
    # Scope claims cover project + explicit grant (standing roots widen the
    # sandbox write set without necessarily claiming every standing path).
    assert scope_paths.realm_of(str(project)) in realms
    assert scope_paths.realm_of(str(granted)) in realms
    assert all(claim.kind == "root" for claim in declared)
    assert str(granted) in json.loads(row["granted_roots"])


def test_explicit_declared_scope_overrides_the_derivation(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    held: list[HeldProcess],
) -> None:
    """The keyword exists for a planner that knows better; nothing passes it yet."""
    set_mode(monkeypatch, "enforce")
    launches: list[list[str]] = []
    supervisor = make_supervisor(sessions_dal, tmp_path, launches, build=held_builder(held))
    realm = scope_paths.realm_of(str(project))
    assert realm is not None
    narrow = [ScopeClaim.for_path(realm, "src/app.py", kind="file")]

    session_id = supervisor.spawn(str(project), "haiku", "do work", declared_scope=narrow)
    rows = live_locks(sessions_dal)
    assert [lock["rel_path"] for lock in rows] == ["src/app.py"]
    assert [lock["kind"] for lock in rows] == ["file"]
    assert [lock["holder_id"] for lock in rows] == [session_id]


def test_shadow_mode_writes_rows_and_records_the_conflict_without_refusing(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shadow is the soak: the collision is measured, the launch still happens."""
    set_mode(monkeypatch, "shadow")
    hold_foreign_scope(sessions_dal, project)
    launches: list[list[str]] = []
    supervisor = make_supervisor(sessions_dal, tmp_path, launches)

    session_id = supervisor.spawn(str(project), "haiku", "do work")

    assert len(launches) == 1
    assert live_waiters(sessions_dal) == []
    row = sessions_dal.get_session(session_id)
    assert row is not None
    assert row["scope_json"] is not None


def test_shadow_mode_gate_reports_the_conflict_it_did_not_refuse(
    sessions_dal: SessionsDal, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observation IS the product of a shadow soak, so it reaches the caller."""
    set_mode(monkeypatch, "shadow")
    hold_foreign_scope(sessions_dal, project)
    gate = SessionScopeGate(sessions_dal, mode_cache_s=0.0)
    decision = gate.open_for_launch("ses_shadow", project_dir=str(project))
    assert decision.outcome == "shadowed"
    assert decision.may_launch is True
    assert decision.holds_locks is True
    assert decision.detail  # the conflict enforcement WOULD have refused


# ---------------------------------------------------------------------------
# Renew
# ---------------------------------------------------------------------------


def test_renew_extends_the_lease_but_not_on_every_poll(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    held: list[HeldProcess],
) -> None:
    """One write per third of the TTL — a 2 Hz loop must not be a write storm."""
    set_mode(monkeypatch, "enforce")
    monkeypatch.setenv("OMNIAGENTOS_SCOPE_TTL_S", "90")
    launches: list[list[str]] = []
    supervisor = make_supervisor(sessions_dal, tmp_path, launches, build=held_builder(held))
    session_id = supervisor.spawn(str(project), "haiku", "do work")

    calls: list[tuple[str, str, int]] = []
    real_renew = PathLockStore.renew_scope

    def counting_renew(
        self: PathLockStore,
        holder_kind: str,
        holder_id: str,
        generation: int,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        calls.append((holder_kind, holder_id, generation))
        return real_renew(self, holder_kind, holder_id, generation, *args, **kwargs)

    monkeypatch.setattr(PathLockStore, "renew_scope", counting_renew)

    row = sessions_dal.get_session(session_id) or {}
    for _ in range(5):
        supervisor._service_session(session_id, row)
    # The grant already armed the interval, so five immediate polls write nothing.
    assert calls == []

    # Age the last-renew mark past a third of the 90s TTL.
    supervisor._scope._last_renew[session_id] = time.monotonic() - 31.0
    supervisor._service_session(session_id, row)
    assert calls == [("session", session_id, 0)]


def test_renew_skips_sessions_this_process_holds_nothing_for(
    sessions_dal: SessionsDal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The held-set is an optimization gate, and this is the case it optimizes."""
    set_mode(monkeypatch, "enforce")
    gate = SessionScopeGate(sessions_dal, mode_cache_s=0.0)

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("renew must not reach the store for an unheld session")

    monkeypatch.setattr(PathLockStore, "renew_scope", explode)
    monkeypatch.setattr(PathLockStore, "release_scope", explode)
    assert gate.renew({"id": "ses_never_claimed"}) is False
    assert gate.release("ses_never_claimed") == 0


def test_a_lapsed_lease_is_re_taken_on_the_next_renew(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    held: list[HeldProcess],
) -> None:
    """A lease that expired under a stalled supervisor is re-declared, not abandoned.

    Silently losing the lock under a session that is still writing is the one
    outcome worse than never having taken it, because a second claimant then
    acquires the same paths entirely legitimately.
    """
    set_mode(monkeypatch, "enforce")
    launches: list[list[str]] = []
    supervisor = make_supervisor(sessions_dal, tmp_path, launches, build=held_builder(held))
    session_id = supervisor.spawn(str(project), "haiku", "do work")

    # The lease lapses (here: released out from under it) while the poll loop was
    # stalled past the whole TTL.
    PathLockStore(sessions_dal).release_scope("session", session_id, 0)
    assert live_locks(sessions_dal) == []
    supervisor._scope._last_renew[session_id] = time.monotonic() - 3600.0

    supervisor._service_session(session_id, sessions_dal.get_session(session_id) or {})

    assert {lock["holder_id"] for lock in live_locks(sessions_dal)} == {session_id}


def test_a_lapsed_lease_someone_else_took_is_not_stolen_back(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    held: list[HeldProcess],
) -> None:
    """If the paths were legitimately re-claimed during the stall, they stay claimed."""
    set_mode(monkeypatch, "enforce")
    launches: list[list[str]] = []
    supervisor = make_supervisor(sessions_dal, tmp_path, launches, build=held_builder(held))
    session_id = supervisor.spawn(str(project), "haiku", "do work")
    PathLockStore(sessions_dal).release_scope("session", session_id, 0)
    hold_foreign_scope(sessions_dal, project, holder_id="run_took_over")
    supervisor._scope._last_renew[session_id] = time.monotonic() - 3600.0

    supervisor._service_session(session_id, sessions_dal.get_session(session_id) or {})

    assert {lock["holder_id"] for lock in live_locks(sessions_dal)} == {"run_took_over"}
    # And it does not retry every poll: the session is out of the held set.
    assert session_id not in supervisor._scope._holding


def test_adopt_reclaims_scope_after_a_supervisor_restart(
    sessions_dal: SessionsDal,
    project: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    held: list[HeldProcess],
) -> None:
    """A fresh supervisor re-takes a live session's declared scope on reconcile."""
    set_mode(monkeypatch, "enforce")
    launches: list[list[str]] = []
    first = make_supervisor(sessions_dal, tmp_path, launches, build=held_builder(held))
    session_id = first.spawn(str(project), "haiku", "do work")
    row = sessions_dal.get_session(session_id)
    assert row is not None and row["scope_json"] is not None
    pid = int(row["pid"])

    # Its locks lapse while the supervisor is down.
    PathLockStore(sessions_dal).release_scope("session", session_id, 0)
    assert live_locks(sessions_dal) == []

    second = make_supervisor(sessions_dal, tmp_path, [])
    second._liveness = lambda candidate: candidate == pid  # type: ignore[assignment]
    second.reconcile()

    assert {lock["holder_id"] for lock in live_locks(sessions_dal)} == {session_id}
    assert second._scope.renew(sessions_dal.get_session(session_id) or {}) is True


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_claims_for_roots_dedupes_and_drops_blank(project: Path, tmp_path: Path) -> None:
    claims = claims_for_roots(
        [
            str(project),
            str(project),  # exact duplicate
            f"{project}/",  # same path, different spelling
            "",  # blank
        ]
    )
    project_realm = scope_paths.realm_of(str(project))
    assert [(claim.realm, claim.path_text) for claim in claims] == [(project_realm, ".")]
    assert all(claim.kind == "root" for claim in claims)


def test_claims_for_roots_drops_a_root_with_no_realm(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unusable grant dir narrows the claim set; it never fails the spawn."""
    monkeypatch.setattr(scope_wiring, "realm_of", lambda _path: None)
    assert claims_for_roots([str(project)]) == ()


def test_scope_json_round_trips(project: Path) -> None:
    claims = claims_for_roots([str(project)])
    payload = encode_scope(claims)
    assert payload is not None
    assert decode_scope(payload) == claims
    assert encode_scope([]) is None
    assert decode_scope(None) == ()
    assert decode_scope("not json") == ()
    assert decode_scope('{"not": "a list"}') == ()
    assert decode_scope('[{"path": "x"}]') == ()  # no realm -> dropped


def test_decision_flags() -> None:
    assert ScopeDecision(outcome="granted").may_launch is True
    assert ScopeDecision(outcome="shadowed").holds_locks is True
    assert ScopeDecision(outcome="parked").may_launch is False
    assert ScopeDecision(outcome="fenced").may_launch is False
    assert ScopeDecision(outcome="off").holds_locks is False
    assert ScopeDecision(outcome="exempt").may_launch is True
