"""H-39 / OA-005 — ordered, idempotent session supervisor close.

Behavioral coverage (not source-text assertions):
- delayed reader/finalizer threads join before DAL close
- repeated close is safe
- concurrent close is safe
- injected DAL latency either completes under budget or fails explicitly
- no DAL access after successful close
- manifests are written or the session is explicitly failed (never silent loss)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omniagentos.sessions import lifecycle as lifecycle_module
from omniagentos.sessions import supervisor as supervisor_module
from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.lifecycle import (
    DEFAULT_JOIN_TIMEOUT_S,
    SHUTDOWN_KILLED_BY,
    AdoptedPidVerdict,
    ProcessObservation,
    SessionSupervisorCloseError,
    command_matches_session,
    observe_process,
    verify_adopted_pid,
)
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.sessions.supervisor import SessionSupervisor

from .conftest import seed_session


def _await_terminal(dal: Any, session_id: str, state: str = "completed") -> None:
    """Block until a session row reaches ``state``, or fail the test."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        row = dal.get_session(session_id)
        if row is not None and row["state"] == state:
            return
        time.sleep(0.01)
    pytest.fail(f"session {session_id} never reached {state}")


def _await_reader_exit(supervisor: SessionSupervisor, session_id: str) -> None:
    """Block until the session's reader/finalizer thread has unregistered.

    Makes the DAL-failure tests deterministic: the supervisor must be provably
    idle for the session before the connection is taken away, so the failure
    under test is the missing manifest and nothing else.
    """
    thread = supervisor._background_threads.get(session_id)
    if thread is not None:
        thread.join(timeout=5.0)
        assert not thread.is_alive(), f"reader for {session_id} never exited"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        with supervisor._lock:
            if session_id not in supervisor._background_threads:
                return
        time.sleep(0.01)
    pytest.fail(f"reader for {session_id} never unregistered")


def _ps_usable() -> bool:
    """Some sandboxes forbid executing ``ps``; the real-process observation test
    is skipped there. The verdict logic itself is covered with injected
    observations, so nothing about H-39 case 5 goes unproven either way."""
    return observe_process(os.getpid()).observed


class FakeProcess:
    next_pid = 70000

    def __init__(
        self,
        lines: list[dict[str, Any]] | None = None,
        returncode: int = 0,
        *,
        release: threading.Event | None = None,
        hold_stdout: bool = False,
    ) -> None:
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self._lines = [json.dumps(line) + "\n" for line in (lines or [])]
        self.returncode: int | None = returncode if not hold_stdout else None
        self._release = release
        self._hold_stdout = hold_stdout
        self.terminated = False
        self.killed = False
        self.stdout = self._stdout_iter()

    def _stdout_iter(self):
        if self._hold_stdout and self._release is not None:
            self._release.wait(5.0)
        yield from self._lines
        if self.returncode is None:
            self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self._hold_stdout and self._release is not None:
            self._release.wait(5.0)
        return int(self.returncode or 0)

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        if self._release is not None:
            self._release.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        if self._release is not None:
            self._release.set()


def _factory(
    captures: list[tuple[list[str], dict[str, Any]]],
    builder: Callable[[], FakeProcess],
) -> Callable[..., Any]:
    def factory(argv: list[str], **kwargs: Any) -> FakeProcess:
        captures.append((argv, kwargs))
        return builder()

    return factory


def _supervisor(
    sessions_dal: Any,
    tmp_path: Path,
    process_builder: Callable[[], FakeProcess],
    *,
    captures: list[tuple[list[str], dict[str, Any]]] | None = None,
) -> SessionSupervisor:
    caps = captures if captures is not None else []
    return SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=_factory(caps, process_builder),
        notifier=lambda _t, _b: None,
        kill_grace=0.05,
    )


def _patch_spawn_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the account/settings lookups spawn does before launching."""
    monkeypatch.setattr(supervisor_module, "_resolve_spawn_account", lambda: (None, {}, None, None))
    monkeypatch.setattr(supervisor_module, "bridge_settings_path", lambda: "/tmp/hooks.json")


def _harmless_child(*extra_argv: str) -> subprocess.Popen[bytes]:
    """Start an isolated, harmless sleeper in its own process group.

    ``extra_argv`` lands verbatim in the child's command line, which is how the
    adopted-PID tests plant (or withhold) launch provenance. Nothing here talks
    to a network or a service.
    """
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", "import time; time.sleep(30)", *extra_argv],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def child_processes() -> Any:
    """Guarantee every test-spawned sleeper is reaped, pass or fail."""
    children: list[subprocess.Popen[bytes]] = []
    yield children
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_lifecycle_module_exports_l17_contract() -> None:
    """L17 attaches through SessionSupervisor.close; this is the stable surface."""
    assert DEFAULT_JOIN_TIMEOUT_S > 0
    assert SHUTDOWN_KILLED_BY
    assert issubclass(SessionSupervisorCloseError, RuntimeError)


def test_close_joins_delayed_finalizer_before_dal_close(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow finalizer must finish (and write the manifest) before DAL close."""
    release = threading.Event()
    entered = threading.Event()
    dal_access_after_close: list[str] = []
    original_get = sessions_dal.get_session
    supervisor_holder: list[SessionSupervisor] = []

    def delayed_finish(self: SessionSupervisor, handle: Any, return_code: int) -> None:
        entered.set()
        # Block finalization until the test arms release — close() must wait.
        assert release.wait(3.0), "finalizer was not released"
        return original_finish(self, handle, return_code)

    original_finish = SessionSupervisor._process_finished
    monkeypatch.setattr(SessionSupervisor, "_process_finished", delayed_finish)

    def tracking_get(session_id: str, *args: Any, **kwargs: Any) -> Any:
        if supervisor_holder and supervisor_holder[0]._dal_closed:
            dal_access_after_close.append(session_id)
        return original_get(session_id, *args, **kwargs)

    process = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        returncode=0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: process)
    supervisor_holder.append(supervisor)
    monkeypatch.setattr(sessions_dal, "get_session", tracking_get)
    monkeypatch.setattr(supervisor_module, "_resolve_spawn_account", lambda: (None, {}, None, None))
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )

    session_id = supervisor.spawn(str(tmp_path), "haiku", "do work")
    assert entered.wait(2.0), "finalizer never entered"

    # Close in a side thread so we can prove it blocks on the finalizer.
    result: dict[str, Any] = {}

    def do_close() -> None:
        try:
            supervisor.close(join_timeout=2.0)
            result["ok"] = True
        except BaseException as exc:  # noqa: BLE001 - captured for assertion
            result["exc"] = exc

    closer = threading.Thread(target=do_close, name="close-under-test")
    closer.start()
    # Give close a chance to reach the join of the delayed finalizer.
    time.sleep(0.1)
    assert closer.is_alive(), "close returned before delayed finalizer finished"
    release.set()
    closer.join(timeout=3.0)
    assert not closer.is_alive()
    assert result.get("ok") is True, result
    assert supervisor.is_closed
    assert supervisor._dal_closed
    assert not dal_access_after_close

    manifest = supervisor.manifest.path_for(session_id)
    assert manifest.exists(), "manifest must be durably written before close returns"
    payload = json.loads(manifest.read_text(encoding="utf-8").splitlines()[0])
    assert payload["final_state"] == "completed"
    assert supervisor._background_threads == {}


def test_close_flushes_terminal_manifest_before_dal_close(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shutdown-before-flush mutation must fail this observed ordering."""
    session_id = seed_session(
        sessions_dal,
        tmp_path,
        session_id="ses_shutdown_flush_order",
        state="completed",
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _title, _body: None,
    )
    order: list[str] = []
    original_write = supervisor.manifest.write
    original_close = sessions_dal.close

    def tracking_write(*args: Any, **kwargs: Any) -> Path:
        assert sessions_dal.get_session(session_id) is not None
        order.append("manifest")
        return original_write(*args, **kwargs)

    def tracking_close() -> None:
        order.append("dal_close")
        original_close()

    monkeypatch.setattr(supervisor.manifest, "write", tracking_write)
    monkeypatch.setattr(sessions_dal, "close", tracking_close)

    supervisor.close()

    assert order == ["manifest", "dal_close"]
    assert supervisor.manifest.path_for(session_id).exists()


def test_close_times_out_without_closing_dal_under_stuck_finalizer(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded shutdown failure is surfaced; DAL stays open so work is not lost."""
    hold = threading.Event()
    entered = threading.Event()

    def stuck_finish(self: SessionSupervisor, handle: Any, return_code: int) -> None:
        entered.set()
        hold.wait(5.0)
        return original_finish(self, handle, return_code)

    original_finish = SessionSupervisor._process_finished
    monkeypatch.setattr(SessionSupervisor, "_process_finished", stuck_finish)
    monkeypatch.setattr(supervisor_module, "_resolve_spawn_account", lambda: (None, {}, None, None))
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )

    process = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        returncode=0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: process)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "stuck finalizer")
    assert entered.wait(2.0), "stuck finalizer never entered"

    # terminate_children=False keeps the reader on the stuck finalizer path
    # (a kill race can skip _process_finished via the _killing flag).
    with pytest.raises(SessionSupervisorCloseError) as info:
        supervisor.close(join_timeout=0.15, terminate_children=False)
    assert session_id in info.value.timed_out_sessions
    assert not supervisor._dal_closed, "DAL must remain open after bounded failure"
    assert not supervisor.is_closed

    # Unblock and retry — second close must succeed and flush the manifest.
    hold.set()
    supervisor.close(join_timeout=2.0, terminate_children=False)
    assert supervisor.is_closed
    assert supervisor._dal_closed
    assert supervisor.manifest.path_for(session_id).exists()


def test_repeated_close_is_idempotent(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor_module, "_resolve_spawn_account", lambda: (None, {}, None, None))
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    process = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        returncode=0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: process)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "idempotent close")

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        row = sessions_dal.get_session(session_id)
        if row is not None and row["state"] == "completed":
            break
        time.sleep(0.01)
    else:
        # Even if state lag, close must still join and finish.
        pass

    supervisor.close(join_timeout=2.0)
    supervisor.close(join_timeout=2.0)
    supervisor.close(join_timeout=0.0)
    assert supervisor.is_closed
    assert supervisor.manifest.path_for(session_id).exists()
    with pytest.raises(RuntimeError, match="shutting down or closed"):
        supervisor.spawn(str(tmp_path), "haiku", "after close")


def test_concurrent_close_is_safe(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    entered = threading.Event()

    def delayed_finish(self: SessionSupervisor, handle: Any, return_code: int) -> None:
        entered.set()
        release.wait(3.0)
        return original_finish(self, handle, return_code)

    original_finish = SessionSupervisor._process_finished
    monkeypatch.setattr(SessionSupervisor, "_process_finished", delayed_finish)
    monkeypatch.setattr(supervisor_module, "_resolve_spawn_account", lambda: (None, {}, None, None))
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )

    process = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        returncode=0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: process)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "concurrent close")
    assert entered.wait(2.0)

    errors: list[BaseException] = []
    barrier = threading.Barrier(3)

    def closer() -> None:
        try:
            barrier.wait(timeout=2.0)
            supervisor.close(join_timeout=3.0)
        except BaseException as exc:  # noqa: BLE001 - collect for assertion
            errors.append(exc)

    threads = [threading.Thread(target=closer) for _ in range(3)]
    for thread in threads:
        thread.start()
    time.sleep(0.05)
    release.set()
    for thread in threads:
        thread.join(timeout=4.0)
        assert not thread.is_alive()

    assert errors == [], f"concurrent close raised: {errors!r}"
    assert supervisor.is_closed
    assert supervisor._dal_closed
    assert supervisor.manifest.path_for(session_id).exists()


def test_injected_dal_latency_still_flushes_manifest(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DAL latency during close-time flush is absorbed by the budget; no silent loss."""
    original_get = sessions_dal.get_session
    original_list = sessions_dal.list_sessions
    original_list_approvals = sessions_dal.list_session_approvals
    supervisor_holder: list[SessionSupervisor] = []

    def slow_when_closing(fn: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if supervisor_holder and supervisor_holder[0]._stop.is_set():
                time.sleep(0.08)
            return fn(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(sessions_dal, "get_session", slow_when_closing(original_get))
    monkeypatch.setattr(sessions_dal, "list_sessions", slow_when_closing(original_list))
    monkeypatch.setattr(
        sessions_dal,
        "list_session_approvals",
        slow_when_closing(original_list_approvals),
    )
    monkeypatch.setattr(supervisor_module, "_resolve_spawn_account", lambda: (None, {}, None, None))
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )

    process = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0.01}],
        returncode=0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: process)
    supervisor_holder.append(supervisor)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "slow dal")

    # Reach terminal state without shutdown pressure so close() exercises the
    # flush path under injected DAL latency rather than a kill race.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        row = sessions_dal.get_session(session_id)
        if row is not None and row["state"] == "completed":
            break
        time.sleep(0.02)
    else:
        pytest.fail("session did not complete before close")

    supervisor.close(join_timeout=3.0, terminate_children=False)

    assert supervisor.is_closed
    path = supervisor.manifest.path_for(session_id)
    assert path.exists(), "manifest must not be silently lost under DAL latency"
    payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["final_state"] == "completed"
    assert payload["session_id"] == session_id


def test_shutdown_joins_but_close_owns_dal(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shutdown() is the signal-safe join path; close() owns DAL teardown."""
    monkeypatch.setattr(supervisor_module, "_resolve_spawn_account", lambda: (None, {}, None, None))
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    process = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        returncode=0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: process)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "shutdown then close")

    supervisor.shutdown(join_timeout=2.0)
    assert supervisor._stop.is_set()
    assert not supervisor._dal_closed
    assert supervisor._background_threads == {}
    assert supervisor.manifest.path_for(session_id).exists()

    supervisor.close(join_timeout=1.0)
    assert supervisor._dal_closed
    assert supervisor.is_closed


def test_close_without_children_is_safe(sessions_dal: Any, tmp_path: Path) -> None:
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _t, _b: None,
    )
    supervisor.close()
    supervisor.close()
    assert supervisor.is_closed
    assert supervisor._dal_closed


def test_close_closes_longhaul_engine_and_rejects_late_terminal_callback(
    sessions_dal: Any,
    tmp_path: Path,
) -> None:
    """Supervisor shutdown owns the lazy longhaul engine's terminal fence."""
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _t, _b: None,
    )
    engine = supervisor._longhaul_engine()
    store = engine.store

    supervisor.close()

    assert engine._terminal_closed is True
    with pytest.raises(RuntimeError, match="LonghaulStore is closed"):
        _ = store._connection

    asyncio.run(
        engine.on_session_terminal(
            {"title": "[longhaul:tks_late]", "state": "failed"}, [], 1
        )
    )
    assert engine._rejected_terminal_callbacks == 1


def test_launch_racing_close_terminates_unregistered_child(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-39 case 1: a child created before registration cannot escape a close.

    Deterministic interleaving (no sleeps deciding the outcome): the launch is
    parked at ``dal.set_pid``, which is *after* the child exists and *before*
    the reader is registered -- precisely the window in which the old code lost
    the child. Close is released only once the launch is provably parked there.
    """
    _patch_spawn_env(monkeypatch)

    child_created = threading.Event()
    at_set_pid = threading.Event()
    release_launch = threading.Event()
    # hold_stdout keeps poll() at None, i.e. the child is still running -- the
    # only state in which "escaping the close" means anything.
    proc = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        hold_stdout=True,
    )

    def factory(argv: list[str], **kwargs: Any) -> FakeProcess:
        child_created.set()
        return proc

    original_set_pid = sessions_dal.set_pid

    def parked_set_pid(session_id: str, pid: int) -> Any:
        # Park OUTSIDE the DAL so close can still read/write the database.
        at_set_pid.set()
        assert release_launch.wait(5.0), "launch was never released"
        return original_set_pid(session_id, pid)

    monkeypatch.setattr(sessions_dal, "set_pid", parked_set_pid)

    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=factory,
        notifier=lambda _t, _b: None,
        kill_grace=0.05,
    )

    spawn_result: dict[str, Any] = {}

    def do_spawn() -> None:
        try:
            spawn_result["session_id"] = supervisor.spawn(str(tmp_path), "haiku", "launch vs close")
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
            spawn_result["error"] = exc

    spawner = threading.Thread(target=do_spawn, name="spawner")
    spawner.start()
    assert child_created.wait(5.0), "process factory never ran"
    assert at_set_pid.wait(5.0), "launch never reached the pre-registration window"

    close_result: dict[str, Any] = {}

    def do_close() -> None:
        try:
            supervisor.close(join_timeout=3.0, close_dal=False)
            close_result["ok"] = True
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted below
            close_result["error"] = exc

    closer = threading.Thread(target=do_close, name="closer")
    closer.start()
    # Release the launch only once close has provably claimed the shutdown gate,
    # so the interleaving under test is the one that is asserted below.
    gate_deadline = time.monotonic() + 5.0
    while time.monotonic() < gate_deadline and not supervisor._stop.is_set():
        time.sleep(0.005)
    assert supervisor._stop.is_set(), "close never reached the shutdown gate"
    release_launch.set()

    spawner.join(timeout=5.0)
    closer.join(timeout=5.0)
    assert not spawner.is_alive()
    assert not closer.is_alive()

    # Registration is refused ...
    error = spawn_result.get("error")
    assert error is not None, "spawn must not report success across a close"
    assert "close in progress" in str(error) or "shutting down" in str(error)
    # ... and the already-created child is reaped rather than leaked.
    assert proc.terminated or proc.killed, "unregistered child escaped the close"
    # The launch drained, so close saw no stranded work.
    assert close_result.get("ok") is True, f"close failed: {close_result}"

    supervisor.close(join_timeout=2.0)
    assert supervisor.is_closed


def test_close_reports_stranded_launch_and_reaps_its_child(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-39 case 1: a launch that outlives the close budget is reported, not lost."""
    _patch_spawn_env(monkeypatch)

    at_set_pid = threading.Event()
    release_launch = threading.Event()
    proc = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        hold_stdout=True,
    )

    original_set_pid = sessions_dal.set_pid

    def parked_set_pid(session_id: str, pid: int) -> Any:
        at_set_pid.set()
        assert release_launch.wait(5.0), "launch was never released"
        return original_set_pid(session_id, pid)

    monkeypatch.setattr(sessions_dal, "set_pid", parked_set_pid)

    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=lambda argv, **kw: proc,
        notifier=lambda _t, _b: None,
        kill_grace=0.05,
    )

    spawn_result: dict[str, Any] = {}

    def do_spawn() -> None:
        try:
            spawn_result["session_id"] = supervisor.spawn(str(tmp_path), "haiku", "stranded launch")
        except BaseException as exc:  # noqa: BLE001
            spawn_result["error"] = exc

    spawner = threading.Thread(target=do_spawn, name="spawner")
    spawner.start()
    assert at_set_pid.wait(5.0), "launch never reached the pre-registration window"

    # Close with a budget the parked launch cannot meet.
    with pytest.raises(SessionSupervisorCloseError) as excinfo:
        supervisor.close(join_timeout=0.2, close_dal=False)

    assert excinfo.value.stranded_launches, "stranded launch was not reported"
    # Close takes ownership of the orphan and reaps it.
    assert proc.terminated or proc.killed, "stranded child was not reaped"
    # A failed close stays retryable: nothing was silently marked done.
    assert not supervisor.is_closed
    assert not supervisor.dal_closed

    release_launch.set()
    spawner.join(timeout=5.0)
    assert not spawner.is_alive()
    assert "error" in spawn_result

    supervisor.close(join_timeout=2.0)
    assert supervisor.is_closed


def test_reader_does_not_resume_across_dal_close(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-39 case 2: a parked reader must not resume onto a closed DAL.

    This is the recovery path a failed close leaves behind: an operator (or the
    test fixture) force-closes the DAL while a reader is still parked on its
    stdout. The reader must break out, must not touch the database, and must
    leave visible evidence instead of raising inside a daemon thread.
    """
    _patch_spawn_env(monkeypatch)

    release = threading.Event()
    proc = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        returncode=0,
        release=release,
        hold_stdout=True,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: proc)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "parked reader")

    reader = supervisor._background_threads[session_id]
    assert reader is not None

    # Force the DAL shut underneath the parked reader.
    supervisor._close_dal()
    assert supervisor.dal_closed
    release.set()

    reader.join(timeout=5.0)
    assert not reader.is_alive(), "reader did not exit after DAL close"
    # The finalizer refused to run and said so, rather than exploding on a
    # closed sqlite connection or silently dropping the session.
    assert supervisor._finalizer_failures.get(session_id) == "dal-closed-before-finalize"

    # A close over a closed DAL reports the unflushed session rather than
    # claiming success.
    with pytest.raises(SessionSupervisorCloseError) as excinfo:
        supervisor.close(join_timeout=1.0, terminate_children=False)
    assert session_id in excinfo.value.failed_manifests


def test_run_once_refuses_after_dal_close(sessions_dal: Any, tmp_path: Path) -> None:
    """H-39 case 2: the poll loop cannot resume onto a closed DAL."""
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _t, _b: None,
    )
    supervisor.close()
    assert supervisor.dal_closed
    with pytest.raises(RuntimeError, match="DAL is closed"):
        supervisor.run_once()


def test_close_from_finalizer_defers_dal_close_until_thread_exits(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-39 case 3: close() on a reader thread defers DAL close to that thread's exit.

    The old behaviour merely skipped the self-join and closed the DAL anyway,
    which pulled the connection out from under the very thread that was still
    running. The DAL must stay usable until the reader actually exits.
    """
    _patch_spawn_env(monkeypatch)

    original_finish = SessionSupervisor._process_finished
    observed: dict[str, Any] = {}
    finalizer_done = threading.Event()

    def finalizer_calls_close(self: SessionSupervisor, handle: Any, return_code: int) -> None:
        original_finish(self, handle, return_code)
        try:
            self.close(join_timeout=1.0)
            observed["close_ok"] = True
        except BaseException as exc:  # noqa: BLE001
            observed["close_error"] = exc
        observed["dal_closed_during"] = self.dal_closed
        observed["dal_close_pending"] = self.dal_close_pending
        observed["longhaul_closed_during"] = longhaul_engine._terminal_closed
        observed["longhaul_close_pending"] = self._longhaul_close_pending
        # The connection must still be usable from this thread.
        try:
            observed["row_readable"] = self.dal.get_session(handle.session_id) is not None
        except BaseException as exc:  # noqa: BLE001
            observed["row_error"] = exc
        finalizer_done.set()

    monkeypatch.setattr(SessionSupervisor, "_process_finished", finalizer_calls_close)

    process = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        returncode=0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: process)
    longhaul_engine = supervisor._longhaul_engine()
    session_id = supervisor.spawn(str(tmp_path), "haiku", "close from finalizer")
    reader = supervisor._background_threads[session_id]
    assert reader is not None

    assert finalizer_done.wait(5.0), "finalizer never finished"
    assert observed.get("close_ok") is True, f"close failed: {observed}"
    assert observed.get("dal_closed_during") is False, "DAL closed under a live reader"
    assert observed.get("dal_close_pending") is True, "DAL close was not deferred"
    assert observed.get("longhaul_closed_during") is False, "longhaul closed under a live reader"
    assert observed.get("longhaul_close_pending") is True, "longhaul close was not deferred"
    assert observed.get("row_readable") is True, f"DAL unusable in finalizer: {observed}"

    reader.join(timeout=5.0)
    assert not reader.is_alive()

    # The last unit out performs the deferred close.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not supervisor.dal_closed:
        time.sleep(0.01)
    assert supervisor.dal_closed, "deferred DAL close never completed"
    assert not supervisor.dal_close_pending
    assert longhaul_engine._terminal_closed is True
    assert supervisor._longhaul_close_pending is False
    assert supervisor.is_closed
    assert supervisor.manifest.path_for(session_id).exists()


def test_crashed_finalizer_residue_is_explicitly_failed(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-39: sessions with crashed finalizers are explicitly marked failed on close."""
    finalizer_crash_session: list[str] = []
    crash_event = threading.Event()

    def crashing_finish(self: SessionSupervisor, handle: Any, return_code: int) -> None:
        finalizer_crash_session.append(handle.session_id)
        crash_event.set()
        raise RuntimeError("simulated finalizer crash")

    monkeypatch.setattr(SessionSupervisor, "_process_finished", crashing_finish)
    monkeypatch.setattr(supervisor_module, "_resolve_spawn_account", lambda: (None, {}, None, None))
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )

    process = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        returncode=0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: process)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "crash test")

    # Wait for the finalizer to crash
    assert crash_event.wait(2.0), "finalizer crash never happened"

    # Wait for the process handle to be removed and thread to exit
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with supervisor._lock:
            handle_gone = session_id not in supervisor._processes
            thread = supervisor._background_threads.get(session_id)
            thread_done = thread is None or not thread.is_alive()
        if handle_gone and thread_done:
            break
        time.sleep(0.01)

    # Check the session state BEFORE close
    row_before = sessions_dal.get_session(session_id)
    assert row_before is not None
    # State should still be running since finalizer crashed before updating it
    assert row_before["state"] == "running", f"unexpected state before close: {row_before['state']}"

    # Close should explicitly fail the session since it's non-terminal
    # Use close_dal=False first so we can inspect the state
    supervisor.close(join_timeout=2.0, terminate_children=False, close_dal=False)

    # The session should now be in a terminal state (failed by shutdown)
    row_after = sessions_dal.get_session(session_id)
    assert row_after is not None
    # _finalize_on_shutdown should have marked it failed
    assert row_after["state"] == "failed", f"unexpected state after close: {row_after['state']}"
    assert row_after.get("killed_by") == SHUTDOWN_KILLED_BY

    # Now actually close the DAL
    supervisor.close(join_timeout=1.0)
    assert supervisor.is_closed


def test_atexit_draining_closes_open_supervisors(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-39: drain_open_supervisors closes live supervisors at exit."""
    from omniagentos.sessions.lifecycle import (
        _OPEN_SUPERVISORS,
        _OPEN_SUPERVISORS_LOCK,
        drain_open_supervisors,
    )

    monkeypatch.setattr(supervisor_module, "_resolve_spawn_account", lambda: (None, {}, None, None))
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )

    process = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        returncode=0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: process)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "atexit test")

    # Wait for completion
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        row = sessions_dal.get_session(session_id)
        if row is not None and row["state"] == "completed":
            break
        time.sleep(0.01)

    # Supervisor should be in the open set
    with _OPEN_SUPERVISORS_LOCK:
        assert supervisor in _OPEN_SUPERVISORS

    # Simulate atexit drain
    drain_open_supervisors(timeout=2.0)

    # Supervisor should be closed and removed from set
    assert supervisor.is_closed
    with _OPEN_SUPERVISORS_LOCK:
        assert supervisor not in _OPEN_SUPERVISORS


def test_manifest_write_failure_makes_close_fail_and_stays_recoverable(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-39 case 4: a failed terminal manifest write is visible and retryable."""
    _patch_spawn_env(monkeypatch)

    process = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        returncode=0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: process)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "manifest failure")

    _await_terminal(sessions_dal, session_id)
    _await_reader_exit(supervisor, session_id)

    # The terminal poll only proves the DB row reached `completed`; `_finish`
    # commits that state BEFORE it writes the manifest. Join the finalizer so
    # the manifest is provably on disk before we unlink it -- otherwise the
    # reader can (re)write it just after the unlink and race the assertions.
    _await_reader_exit(supervisor, session_id)

    # Remove the manifest the happy path already wrote so close must rewrite it,
    # then make that rewrite fail.
    manifest_path = supervisor.manifest.path_for(session_id)
    assert manifest_path.exists(), "happy path never wrote the manifest"
    manifest_path.unlink()

    faults = {"active": True}
    original_write = supervisor.manifest.write

    def failing_write(*args: Any, **kwargs: Any) -> Any:
        if faults["active"]:
            raise OSError("injected manifest write failure")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(supervisor.manifest, "write", failing_write)

    with pytest.raises(SessionSupervisorCloseError) as excinfo:
        supervisor.close(join_timeout=2.0, terminate_children=False)
    assert session_id in excinfo.value.failed_manifests
    # Visibly failed => not closed, DAL still open, retry still possible.
    assert not supervisor.is_closed
    assert not supervisor.dal_closed
    assert not manifest_path.exists()

    # Recoverable: once the fault clears, a retry completes the close.
    faults["active"] = False
    supervisor.close(join_timeout=2.0, terminate_children=False)
    assert supervisor.is_closed
    assert supervisor.dal_closed
    assert manifest_path.exists(), "retry did not recover the lost manifest"


def test_crashed_finalizer_manifest_failure_is_reported(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-39 case 4: the newly-terminalized crashed-finalizer session is checked too.

    The stale-state gate used to skip the manifest verification for exactly this
    shape -- the row was ``running`` in the pre-``_finish`` snapshot -- so close
    reported success over a session that never got a manifest.
    """
    _patch_spawn_env(monkeypatch)

    crashed = threading.Event()

    def crashing_finish(self: SessionSupervisor, handle: Any, code: int) -> None:
        crashed.set()
        raise RuntimeError("injected finalizer crash")

    monkeypatch.setattr(SessionSupervisor, "_process_finished", crashing_finish)

    process = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        returncode=0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: process)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "crashed finalizer manifest")

    assert crashed.wait(3.0), "finalizer never crashed"
    reader = supervisor._background_threads.get(session_id)
    if reader is not None:
        reader.join(timeout=3.0)

    assert sessions_dal.get_session(session_id)["state"] == "running"

    faults = {"active": True}
    original_write = supervisor.manifest.write

    def failing_write(*args: Any, **kwargs: Any) -> Any:
        if faults["active"]:
            raise OSError("injected manifest write failure")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(supervisor.manifest, "write", failing_write)

    with pytest.raises(SessionSupervisorCloseError) as excinfo:
        supervisor.close(join_timeout=2.0, terminate_children=False)
    assert session_id in excinfo.value.failed_manifests
    # The session was still terminalized, so the failure is about the manifest
    # only -- and it is reported rather than swallowed.
    row = sessions_dal.get_session(session_id)
    assert row["state"] == "failed"
    assert row.get("killed_by") == SHUTDOWN_KILLED_BY
    assert not supervisor.manifest.path_for(session_id).exists()
    assert not supervisor.is_closed
    assert not supervisor.dal_closed

    faults["active"] = False
    supervisor.close(join_timeout=2.0, terminate_children=False)
    assert supervisor.is_closed
    assert supervisor.manifest.path_for(session_id).exists()


def test_command_provenance_requires_a_whole_token() -> None:
    """H-39 case 5: substring luck must never read as provenance."""
    ref = str(uuid.uuid4())
    assert command_matches_session(f"node /x/cli.js --session-id {ref}", session_ref=ref)
    assert command_matches_session(f"node /x/cli.js --session-id={ref}", session_ref=ref)
    # A longer token that merely contains the ref is a different session.
    assert not command_matches_session(f"node /x/cli.js --session-id {ref}-shadow", session_ref=ref)
    assert not command_matches_session("node /x/cli.js --session-id other", session_ref=ref)
    # Provenance too weak to distinguish anything is refused outright.
    assert not command_matches_session("node /x/cli.js --session-id abc", session_ref="abc")


def _observer_returning(observation: ProcessObservation) -> Any:
    def observer(pid: int, **kwargs: Any) -> ProcessObservation:
        return observation

    return observer


def test_adopted_pid_with_session_ref_in_argv_is_owned() -> None:
    """H-39 case 5: durable provenance in the full command line proves ownership."""
    ref = str(uuid.uuid4())
    decision = verify_adopted_pid(
        4321,
        session_ref=ref,
        session_id="ses_owned",
        observer=_observer_returning(
            ProcessObservation(
                pid=4321,
                observed=True,
                alive=True,
                command=f"node /opt/claude/cli.js --session-id {ref} --model haiku",
            )
        ),
    )
    assert decision.verdict is AdoptedPidVerdict.OWNED
    assert decision.may_signal


def test_adopted_pid_without_provenance_is_foreign() -> None:
    """H-39 case 5: a recycled PID running something else is FOREIGN, never OWNED."""
    decision = verify_adopted_pid(
        4321,
        session_ref=str(uuid.uuid4()),
        session_id="ses_foreign",
        observer=_observer_returning(
            ProcessObservation(
                pid=4321, observed=True, alive=True, command="/usr/bin/rsync -av /a /b"
            )
        ),
    )
    assert decision.verdict is AdoptedPidVerdict.FOREIGN
    assert not decision.may_signal


def test_adopted_pid_claude_without_our_ref_is_foreign() -> None:
    """H-39 case 5: someone else's Claude session is still not ours to kill."""
    decision = verify_adopted_pid(
        4321,
        session_ref=str(uuid.uuid4()),
        session_id="ses_foreign",
        claude_binary="/opt/claude/cli.js",
        observer=_observer_returning(
            ProcessObservation(
                pid=4321,
                observed=True,
                alive=True,
                command=f"node /opt/claude/cli.js --session-id {uuid.uuid4()}",
            )
        ),
    )
    assert decision.verdict is AdoptedPidVerdict.FOREIGN
    assert not decision.may_signal


def test_adopted_pid_observation_failure_is_unknown() -> None:
    """H-39 case 5: an unobservable process fails closed."""
    decision = verify_adopted_pid(
        4321,
        session_ref=str(uuid.uuid4()),
        observer=_observer_returning(
            ProcessObservation(
                pid=4321, observed=False, alive=False, error="OSError: ps unavailable"
            )
        ),
    )
    assert decision.verdict is AdoptedPidVerdict.UNKNOWN
    assert not decision.may_signal


def test_adopted_pid_without_usable_ref_is_unknown() -> None:
    """H-39 case 5: a row with no durable provenance can never authorise a kill."""
    for ref in ("", "abc"):
        decision = verify_adopted_pid(4321, session_ref=ref)
        assert decision.verdict is AdoptedPidVerdict.UNKNOWN
        assert not decision.may_signal


def test_dead_pid_is_gone_not_owned() -> None:
    """H-39 case 5: a dead PID is GONE, never OWNED."""
    ref = str(uuid.uuid4())
    decision = verify_adopted_pid(
        4321,
        session_ref=ref,
        observer=_observer_returning(ProcessObservation(pid=4321, observed=True, alive=False)),
    )
    assert decision.verdict is AdoptedPidVerdict.GONE
    assert not decision.may_signal


@pytest.mark.parametrize(
    "observation",
    [
        # Recycled PID: alive, but demonstrably not ours.
        ProcessObservation(pid=0, observed=True, alive=True, command="/usr/bin/rsync -av /a /b"),
        # Observation failed: no evidence of ownership, so no signal.
        ProcessObservation(pid=0, observed=False, alive=False, error="ps unavailable"),
    ],
    ids=["recycled-pid", "observation-failure"],
)
def test_unverified_adopted_pid_is_never_signalled(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_processes: list[subprocess.Popen[bytes]],
    observation: ProcessObservation,
) -> None:
    """H-39 case 5: a real, unrelated process survives the adopted-kill path."""
    child = _harmless_child("--unrelated-workload")
    child_processes.append(child)

    monkeypatch.setattr(
        supervisor_module,
        "observe_process",
        _observer_returning(
            ProcessObservation(
                pid=child.pid,
                observed=observation.observed,
                alive=observation.alive,
                command=observation.command,
                error=observation.error,
            )
        ),
    )

    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _t, _b: None,
        kill_grace=0.05,
    )
    signalled = supervisor._terminate_adopted_pid(
        child.pid, {"id": "ses_x", "session_ref": str(uuid.uuid4())}
    )
    assert signalled is False
    assert child.poll() is None, "an unrelated process was signalled"
    supervisor.close()


def test_managed_child_is_killed_without_an_identity_gate(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_processes: list[subprocess.Popen[bytes]],
) -> None:
    """H-39 case 5: an owned child must never be leaked by a failing observation.

    A managed ``Popen`` is ours by construction, so termination must not consult
    ``ps`` at all -- otherwise a transient observation failure leaks a real
    Node/Claude session.
    """
    child = _harmless_child("--no-provenance-at-all")
    child_processes.append(child)

    def exploding_observer(pid: int, **kwargs: Any) -> ProcessObservation:
        raise AssertionError("managed children must not be identity-gated")

    monkeypatch.setattr(supervisor_module, "observe_process", exploding_observer)

    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _t, _b: None,
        kill_grace=0.5,
    )
    supervisor._terminate_child_process(child)
    assert child.poll() is not None, "owned child leaked"
    supervisor.close()


def test_reconcile_refuses_to_adopt_a_recycled_pid(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_processes: list[subprocess.Popen[bytes]],
) -> None:
    """H-39 case 5: a PID positively observed as somebody else's is not adopted.

    FOREIGN is the only verdict that blocks adoption, because it is the only one
    that is *evidence* the number was recycled. The session is settled instead of
    being left running against a process we do not own, and the real process on
    the other end of that number is not touched.
    """
    child = _harmless_child("--unrelated-workload")
    child_processes.append(child)

    monkeypatch.setattr(
        supervisor_module,
        "observe_process",
        _observer_returning(
            ProcessObservation(
                pid=child.pid,
                observed=True,
                alive=True,
                command="/usr/bin/rsync -av /a /b",
            )
        ),
    )

    session_id = seed_session(sessions_dal, tmp_path, state="running", pid=child.pid)
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _t, _b: None,
        kill_grace=0.05,
    )
    supervisor._reconcile_session(session_id, sessions_dal.get_session(session_id))

    assert session_id not in supervisor._adopted, "adopted a recycled PID"
    assert sessions_dal.get_session(session_id)["state"] == "killed"
    assert child.poll() is None, "an unrelated process was signalled"
    supervisor.close()


def test_reconcile_adopts_an_unobservable_pid_but_never_signals_it(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_processes: list[subprocess.Popen[bytes]],
) -> None:
    """H-39 case 5: UNKNOWN must not silently drop a live session's bookkeeping.

    Adoption is bookkeeping, not a lethal act, so failing to *read* a live
    process is not grounds for abandoning its session. The fail-closed rule
    applies to the lethal act: with the same unreadable observation, the kill
    path refuses to signal.
    """
    child = _harmless_child("--unreadable-workload")
    child_processes.append(child)

    monkeypatch.setattr(
        supervisor_module,
        "observe_process",
        _observer_returning(
            ProcessObservation(pid=child.pid, observed=False, alive=False, error="ps unavailable")
        ),
    )

    session_id = seed_session(sessions_dal, tmp_path, state="running", pid=child.pid)
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _t, _b: None,
        kill_grace=0.05,
    )
    supervisor._reconcile_session(session_id, sessions_dal.get_session(session_id))

    assert supervisor._adopted.get(session_id) == child.pid
    assert sessions_dal.get_session(session_id)["state"] == "running"

    sessions_dal.request_kill(session_id)
    supervisor._kill_session(session_id)

    assert child.poll() is None, "an unverifiable PID was signalled"
    # F001 (round-3 repair, receipt 5283694621): an unverifiable signal is not
    # a confirmed kill -- terminalizing the row here would report a dead
    # process the supervisor never proved it touched. The row must stay
    # non-terminal so a later cancel read-back reports kill_pending /
    # kill_complete=false, fail closed.
    row = sessions_dal.get_session(session_id)
    assert row["state"] == "running", (
        "an unconfirmed adopted-pid kill terminalized the session row",
        row,
    )
    # F007 (round-4, receipt 5284671912): the fail-closed path must NOT release
    # the adopted-PID context -- a retry needs it to re-enter verification. The
    # round-3 finally released scope/adopted unconditionally, so a SECOND kill
    # attempt lost the very context verification requires.
    assert supervisor._adopted.get(session_id) == child.pid, (
        "fail-closed kill released the adopted-PID context; a retry cannot verify"
    )
    assert session_id not in supervisor._killing

    # A second attempt behaves identically: refuses to signal, row stays
    # non-terminal, context survives for the next retry.
    supervisor._kill_session(session_id)
    assert child.poll() is None, "an unverifiable PID was signalled on retry"
    assert sessions_dal.get_session(session_id)["state"] == "running"
    assert supervisor._adopted.get(session_id) == child.pid

    # The session is legitimately still non-terminal (fail-closed) -- close()
    # correctly refuses to silently drop it rather than pretend it flushed a
    # manifest for a session it never confirmed dead.
    with pytest.raises(SessionSupervisorCloseError) as excinfo:
        supervisor.close()
    assert session_id in excinfo.value.failed_manifests


def test_observe_process_requests_untruncated_command_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deterministic argv proof remains active where real ``ps`` is forbidden."""
    captured: list[str] = []
    marker = "marker-" + "x" * 320

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.extend(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=f"node wrapper {marker}\n", stderr="")

    monkeypatch.setattr(lifecycle_module.subprocess, "run", fake_run)

    observation = observe_process(4242)

    assert captured == ["ps", "-ww", "-o", "command=", "-p", "4242"]
    assert observation.command.endswith(marker)


@pytest.mark.skipif(not _ps_usable(), reason="`ps` is not executable in this sandbox")
def test_observe_process_reads_the_full_command_line(
    child_processes: list[subprocess.Popen[bytes]],
) -> None:
    """Provenance lives in argv, so ``comm``-only observation is not enough.

    The end-to-end check against a real process; the verdict logic above is
    covered deterministically with injected observations.
    """
    marker = f"marker-{uuid.uuid4()}"
    child = _harmless_child("--session-id", marker)
    child_processes.append(child)

    observation = observe_process(child.pid)
    assert observation.observed
    assert observation.alive
    assert marker in observation.command, "observation lost the command arguments"
    assert command_matches_session(observation.command, session_ref=marker)


def test_atexit_drain_records_recovery_evidence(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-39 case 6: a drain that cannot finish leaves evidence and stays retryable."""
    from omniagentos.sessions.lifecycle import (
        _OPEN_SUPERVISORS,
        _OPEN_SUPERVISORS_LOCK,
        drain_failures,
        drain_open_supervisors,
        reset_drain_failures,
    )

    _patch_spawn_env(monkeypatch)
    reset_drain_failures()

    process = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        returncode=0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: process)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "drain evidence")

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        row = sessions_dal.get_session(session_id)
        if row is not None and row["state"] == "completed":
            break
        time.sleep(0.01)

    manifest_path = supervisor.manifest.path_for(session_id)
    if manifest_path.exists():
        manifest_path.unlink()

    faults = {"active": True}
    original_write = supervisor.manifest.write

    def failing_write(*args: Any, **kwargs: Any) -> Any:
        if faults["active"]:
            raise OSError("injected manifest write failure")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(supervisor.manifest, "write", failing_write)

    failures = drain_open_supervisors(timeout=1.0)

    assert failures, "a failed drain reported nothing"
    assert session_id in failures[0].failed_manifests
    # The same evidence is retrievable after the fact (atexit has no caller).
    assert any(session_id in f.failed_manifests for f in drain_failures())
    # Retryable: the supervisor stays registered and unclosed.
    assert not supervisor.is_closed
    with _OPEN_SUPERVISORS_LOCK:
        assert supervisor in _OPEN_SUPERVISORS

    faults["active"] = False
    assert drain_open_supervisors(timeout=2.0) == []
    assert supervisor.is_closed
    assert manifest_path.exists()
    with _OPEN_SUPERVISORS_LOCK:
        assert supervisor not in _OPEN_SUPERVISORS
    reset_drain_failures()


# ---------------------------------------------------------------------------
# H-39: a close over an unusable DAL must still report what it owes
#
# The shape both tests below drive is the one the previous repair reported
# success over: an owned session that finished CLEANLY -- no timeout, no
# crashed finalizer, no stranded launch -- whose manifest is then gone. Neither
# `timed_out` nor `_finalizer_failures` contains it, so with the DAL unusable
# the close had nothing to look at and returned `is_closed=True` with no
# manifest on disk. The obligation ledger is the identity that closes that hole.
# ---------------------------------------------------------------------------


def test_close_over_closed_dal_reports_owed_manifest_of_idle_session(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Supervisor `_close_dal()` first, then close: the owed manifest is reported.

    Nothing about this session misbehaved -- it completed, its finalizer wrote
    the manifest, its reader exited. Deleting the manifest afterwards leaves an
    obligation the DAL can no longer be asked about, and a close that claims
    success over it is the fail-open this test exists to prevent.
    """
    _patch_spawn_env(monkeypatch)

    process = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        returncode=0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: process)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "owed manifest, dead dal")
    _await_terminal(sessions_dal, session_id)
    _await_reader_exit(supervisor, session_id)

    row = sessions_dal.get_session(session_id)
    manifest_path = supervisor.manifest.path_for(session_id)
    assert manifest_path.exists(), "happy path never wrote the manifest"
    manifest_path.unlink()

    supervisor._close_dal()
    assert supervisor.dal_closed
    assert not supervisor._finalizer_failures, "session did not finish cleanly"

    with caplog.at_level(logging.ERROR, logger="omniagentos.sessions.supervisor"):
        with pytest.raises(SessionSupervisorCloseError) as excinfo:
            supervisor.close(join_timeout=1.0, terminate_children=False)
    assert excinfo.value.failed_manifests == [session_id]
    assert not excinfo.value.timed_out_sessions
    assert not excinfo.value.stranded_launches
    assert not supervisor.is_closed
    assert not manifest_path.exists()

    # The loss is logged with enough identity to act on after the process exits:
    # the session id alone cannot be resolved back to a project once the DAL is
    # gone, which is why the obligation carries the row detail.
    evidence = [rec.getMessage() for rec in caplog.records if "owed manifest" in rec.getMessage()]
    assert evidence, "a close that lost a manifest left no log evidence"
    assert any(row["session_ref"] in message for message in evidence)
    assert any(row["project_dir"] in message for message in evidence)

    # Identity survived the connection: the supervisor can still say which
    # sessions it owned and exactly which manifest it owes, with the row detail
    # captured while the DAL still worked.
    assert session_id in supervisor.owned_session_ids
    owed = {obligation.session_id: obligation for obligation in supervisor.manifest_obligations()}
    assert session_id in owed
    assert owed[session_id].final_state == "completed"
    assert owed[session_id].session_ref == row["session_ref"]
    assert owed[session_id].project_dir == row["project_dir"]

    # Retry is truthful, not merely idempotent: the DAL is still dead and the
    # manifest is still missing, so a second close must fail the same way rather
    # than latching "already closed".
    with pytest.raises(SessionSupervisorCloseError) as retry:
        supervisor.close(join_timeout=1.0, terminate_children=False)
    assert retry.value.failed_manifests == [session_id]
    assert not supervisor.is_closed

    # Restart recovery is the real remedy, and it works: a fresh supervisor over
    # the same database and ledger closes cleanly and writes the lost manifest.
    recovery_dal = SessionsDal(tmp_path / "sessions.db")
    recovery = SessionSupervisor(
        recovery_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _t, _b: None,
    )
    recovery.close(join_timeout=2.0, terminate_children=False)
    assert recovery.is_closed
    assert manifest_path.exists(), "restart did not recover the owed manifest"


def test_close_over_externally_broken_dal_reports_owed_manifest(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_processes: Any,
) -> None:
    """The DAL breaks underneath the supervisor: close still fails closed.

    Here ``dal_closed`` is False -- the supervisor never closed anything, the
    connection simply stopped working -- so the flag-guarded branch does not
    even run and every DAL call raises. The close must report the owed manifest
    from the ledger, and must not signal a child it could not prove is OWNED
    (it cannot read the row at all, so no verdict is available).
    """
    _patch_spawn_env(monkeypatch)

    process = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        returncode=0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: process)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "owed manifest, broken dal")
    _await_terminal(sessions_dal, session_id)
    _await_reader_exit(supervisor, session_id)

    manifest_path = supervisor.manifest.path_for(session_id)
    assert manifest_path.exists()
    manifest_path.unlink()

    # A second session, adopted through the real reconcile path off a live but
    # unobservable PID -- the UNKNOWN verdict that adoption tolerates and the
    # kill path refuses.
    stranger = _harmless_child()
    child_processes.append(stranger)
    adopted_id = seed_session(
        sessions_dal,
        tmp_path,
        session_id="ses_adopted_stranger",
        state="running",
        pid=stranger.pid,
    )
    monkeypatch.setattr(
        supervisor_module,
        "observe_process",
        _observer_returning(
            ProcessObservation(
                pid=stranger.pid, observed=False, alive=False, error="ps unavailable"
            )
        ),
    )
    supervisor._reconcile_session(adopted_id, sessions_dal.get_session(adopted_id))
    assert supervisor._adopted.get(adopted_id) == stranger.pid, "adoption did not stick"

    # Break the connection from outside the supervisor's lifecycle.
    sessions_dal.close()
    assert not supervisor.dal_closed, "test must not use the supervisor's own close"

    with pytest.raises(SessionSupervisorCloseError) as excinfo:
        supervisor.close(join_timeout=1.0, terminate_children=True)
    assert session_id in excinfo.value.failed_manifests
    assert not supervisor.is_closed
    assert not manifest_path.exists()

    # Identity and obligations remain readable over the dead connection.
    assert session_id in supervisor.owned_session_ids
    assert adopted_id in supervisor.owned_session_ids
    assert session_id in {o.session_id for o in supervisor.manifest_obligations()}

    # A close that cannot read the session row cannot have obtained an OWNED
    # verdict, so the adopted PID must be untouched -- terminate_children=True
    # is a request, never a licence to signal on no evidence.
    time.sleep(0.2)
    assert stranger.poll() is None, "close signalled a PID it could not prove was ours"


def test_obligation_ledger_does_not_claim_sessions_this_supervisor_never_owned(
    sessions_dal: Any,
    tmp_path: Path,
) -> None:
    """The fix must fail closed on our sessions, not on everybody's.

    ``reconcile`` walks every terminal row in the database and backfills missing
    manifests. That is housekeeping, not ownership: a manifest for a session
    this supervisor never launched or adopted belongs to whoever ran it, and
    turning each one into a close-blocking obligation would make every shutdown
    hostage to unrelated ledger history.
    """
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _t, _b: None,
    )
    foreign_id = seed_session(sessions_dal, tmp_path, session_id="ses_foreign", state="completed")
    supervisor.reconcile()
    foreign_manifest = supervisor.manifest.path_for(foreign_id)
    assert foreign_manifest.exists(), "reconcile did not backfill the manifest"
    foreign_manifest.unlink()

    assert supervisor.manifest_obligations() == []
    assert foreign_id not in supervisor.owned_session_ids

    supervisor._close_dal()
    supervisor.close(join_timeout=1.0, terminate_children=False)
    assert supervisor.is_closed


def test_owed_manifest_deleted_after_a_successful_write_is_still_owed(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Satisfaction is proved from disk, never from a "written" flag.

    A ledger that recorded "written" once and trusted it would report success
    for exactly the repro shape: manifest written by the happy path, then
    removed. The check must be a stat at close time.
    """
    _patch_spawn_env(monkeypatch)

    process = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        returncode=0,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: process)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "written then deleted")
    _await_terminal(sessions_dal, session_id)
    _await_reader_exit(supervisor, session_id)

    manifest_path = supervisor.manifest.path_for(session_id)
    assert manifest_path.exists()
    # The manifest exists, so nothing is owed right now.
    assert supervisor._unsatisfied_manifest_obligations() == set()

    manifest_path.unlink()
    assert supervisor._unsatisfied_manifest_obligations() == {session_id}

    # With the DAL alive the close repairs it instead of failing -- fail-closed
    # means "never claim success over a loss", not "never recover".
    supervisor.close(join_timeout=2.0, terminate_children=False)
    assert supervisor.is_closed
    assert manifest_path.exists()


def test_close_over_closed_dal_reports_owed_manifest_of_running_owned_session(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An owned launched session that is still RUNNING when the DAL is closed must fail close."""
    _patch_spawn_env(monkeypatch)

    proc = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        # Real, never-set release => the reader genuinely parks and the session
        # stays owed; without it the reader finalizes early and races
        # _close_dal(), so close() can find nothing owed (flaky DID NOT RAISE).
        release=threading.Event(),
        hold_stdout=True,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: proc)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "running owned session")

    # Close DAL while the session is still running
    supervisor._close_dal()
    assert supervisor.dal_closed

    with pytest.raises(SessionSupervisorCloseError) as excinfo:
        supervisor.close(join_timeout=1.0, terminate_children=False)

    assert session_id in excinfo.value.failed_manifests
    assert not supervisor.is_closed
    assert session_id in supervisor.owned_session_ids
    assert not supervisor.manifest.path_for(session_id).exists()


def test_close_over_closed_dal_reports_owed_manifest_of_running_adopted_session(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_processes: list[subprocess.Popen[bytes]],
) -> None:
    """An adopted session still RUNNING when the DAL is closed must fail close."""
    _patch_spawn_env(monkeypatch)

    child = _harmless_child("--adopted-running")
    child_processes.append(child)

    monkeypatch.setattr(
        supervisor_module,
        "observe_process",
        _observer_returning(
            ProcessObservation(pid=child.pid, observed=False, alive=False, error="ps unavailable")
        ),
    )

    adopted_id = seed_session(
        sessions_dal,
        tmp_path,
        session_id="ses_adopted_running",
        state="running",
        pid=child.pid,
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _t, _b: None,
        kill_grace=0.05,
    )
    supervisor._reconcile_session(adopted_id, sessions_dal.get_session(adopted_id))
    assert supervisor._adopted.get(adopted_id) == child.pid

    supervisor._close_dal()
    assert supervisor.dal_closed

    with pytest.raises(SessionSupervisorCloseError) as excinfo:
        supervisor.close(join_timeout=1.0, terminate_children=False)

    assert adopted_id in excinfo.value.failed_manifests
    assert not supervisor.is_closed
    assert adopted_id in supervisor.owned_session_ids


def test_close_over_externally_broken_dal_reports_owed_manifest_of_running_session(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An owned running session over an externally broken DAL must fail close."""
    _patch_spawn_env(monkeypatch)

    proc = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        # Real, never-set release => the reader genuinely parks and the session
        # stays owed; without it the reader finalizes early and races the
        # external DAL close, so close() can find nothing owed (flaky).
        release=threading.Event(),
        hold_stdout=True,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: proc)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "running session broken dal")

    # Externally break DAL connection
    sessions_dal.close()

    with pytest.raises(SessionSupervisorCloseError) as excinfo:
        supervisor.close(join_timeout=1.0, terminate_children=False)

    assert session_id in excinfo.value.failed_manifests
    assert not supervisor.is_closed
    assert session_id in supervisor.owned_session_ids


def test_live_dal_explicitly_fails_running_session_and_flushes_manifest(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live DAL explicitly terminalizes a running session and writes its manifest."""
    _patch_spawn_env(monkeypatch)

    proc = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        hold_stdout=True,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: proc)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "live dal running")

    supervisor.close(join_timeout=2.0, terminate_children=True)

    assert supervisor.is_closed
    manifest_path = supervisor.manifest.path_for(session_id)
    assert manifest_path.exists()


def test_repeated_close_over_closed_dal_keeps_failing_until_restart(
    sessions_dal: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated close calls over closed DAL do not latch false success."""
    _patch_spawn_env(monkeypatch)

    proc = FakeProcess(
        [{"type": "result", "subtype": "success", "total_cost_usd": 0}],
        # Real, never-set release => the reader genuinely parks and the session
        # stays owed; without it the reader finalizes early and races
        # _close_dal(), so close() can find nothing owed (flaky DID NOT RAISE).
        release=threading.Event(),
        hold_stdout=True,
    )
    supervisor = _supervisor(sessions_dal, tmp_path, lambda: proc)
    session_id = supervisor.spawn(str(tmp_path), "haiku", "repeated close test")

    supervisor._close_dal()

    with pytest.raises(SessionSupervisorCloseError):
        supervisor.close(join_timeout=1.0, terminate_children=False)
    assert not supervisor.is_closed

    with pytest.raises(SessionSupervisorCloseError):
        supervisor.close(join_timeout=1.0, terminate_children=False)
    assert not supervisor.is_closed

    # Recovery restart
    recovery_dal = SessionsDal(tmp_path / "sessions.db")
    recovery = SessionSupervisor(
        recovery_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _t, _b: None,
    )
    recovery.reconcile()
    recovery.close(join_timeout=2.0, terminate_children=True)
    assert recovery.is_closed
    assert recovery.manifest.path_for(session_id).exists()


def test_foreign_running_sessions_are_discriminated_and_not_owed(
    sessions_dal: Any,
    tmp_path: Path,
) -> None:
    """Foreign/unowned running sessions do not create close obligations."""
    foreign_id = seed_session(
        sessions_dal, tmp_path, session_id="ses_foreign_running", state="running"
    )
    supervisor = SessionSupervisor(
        sessions_dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        notifier=lambda _t, _b: None,
    )
    supervisor.reconcile()

    assert foreign_id not in supervisor.owned_session_ids
    assert supervisor.manifest_obligations() == []

    supervisor._close_dal()
    supervisor.close(join_timeout=1.0, terminate_children=False)
    assert supervisor.is_closed
