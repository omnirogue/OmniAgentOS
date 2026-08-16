"""Real runner coverage for monitored orchestrator executor sessions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omniagentos.intake.fable import FABLE_MODEL
from omniagentos.intake.planner import PlannedTask
from omniagentos.orchestrator.contracts import ExecutorRequest, ExecutorTier
from omniagentos.orchestrator.core import Orchestrator
from omniagentos.orchestrator.executor import (
    CheapAdapterRunner,
    FusionSessionRunner,
    _MonitoredSessionRunner,
)
from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.lifecycle import (
    _OPEN_SUPERVISORS,
    _OPEN_SUPERVISORS_LOCK,
    SessionSupervisorCloseError,
)
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.sessions.supervisor import SessionSupervisor


class _FakeProcess:
    next_pid = 61000

    def __init__(self) -> None:
        self.pid = _FakeProcess.next_pid
        _FakeProcess.next_pid += 1
        self.stdout = [
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "result": "executor finished",
                    "total_cost_usd": 0.01,
                }
            )
            + "\n"
        ]

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0

    def poll(self) -> int:
        return 0

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None


def _request(tmp_path: Path, tier: ExecutorTier, *, model: str | None = None) -> ExecutorRequest:
    return ExecutorRequest(
        task=PlannedTask(title="Create proof", acceptance_criteria=["proof exists"]),
        prompt="Create the proof file.",
        working_dir=str(tmp_path),
        tier=tier,
        model=model,
        lane="superfast" if tier is ExecutorTier.CHEAP else "fusion",
        title="Create proof",
        budget_usd_max=1.0,
    )


@pytest.mark.parametrize(
    ("runner_type", "tier", "expected_model"),
    [
        (CheapAdapterRunner, ExecutorTier.CHEAP, "sonnet"),
        (FusionSessionRunner, ExecutorTier.FUSION, FABLE_MODEL),
    ],
)
def test_real_runner_supervisor_and_dal_complete_owned_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_type: type[CheapAdapterRunner] | type[FusionSessionRunner],
    tier: ExecutorTier,
    expected_model: str,
) -> None:
    """Exercise the real runner + supervisor + DAL, replacing only the CLI process."""
    captures: list[list[str]] = []

    def process_factory(argv: list[str], **_kwargs: Any) -> _FakeProcess:
        captures.append(argv)
        return _FakeProcess()

    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.bridge_settings_path", lambda: "/tmp/hooks.json"
    )
    monkeypatch.setattr("omniagentos.sessions.supervisor.sandbox.sandbox_available", lambda: True)
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.sandbox.wrap_command",
        lambda argv, *_args, **_kwargs: argv,
    )
    dal = SessionsDal(tmp_path / "sessions.db")
    supervisor = SessionSupervisor(
        dal,
        manifest=SessionManifest(tmp_path / "ledger"),
        process_factory=process_factory,  # type: ignore[arg-type]
        notifier=lambda _title, _body: None,
    )
    runner = runner_type(
        spawner=supervisor,
        session_store=dal,
        timeout_seconds=2,
        poll_interval_seconds=0.001,
    )

    result = runner.run(_request(tmp_path, tier), object())  # type: ignore[arg-type]

    assert result.status == "ok"
    assert result.session_id is not None
    row = dal.get_session(result.session_id)
    assert row is not None
    assert row["state"] == "completed"
    assert row["model"] == expected_model
    assert dal.is_orchestrator_session(result.session_id)
    model_index = captures[0].index("--model")
    assert captures[0][model_index + 1] == expected_model


class _Spawner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def spawn(
        self,
        project_dir: str,
        model: str,
        prompt: str,
        budget_usd_max: float | None = None,
        title: str | None = None,
        extra_write_roots: list[str] | None = None,
        title_prefix: str | None = None,
        resume_session_ref: str | None = None,
        orchestrator_owned: bool = False,
        orchestrator_run_id: str | None = None,
        granted_roots: list[str] | None = None,
    ) -> str:
        self.calls.append(
            {
                "project_dir": project_dir,
                "model": model,
                "prompt": prompt,
                "budget_usd_max": budget_usd_max,
                "title": title,
                "extra_write_roots": extra_write_roots,
                "title_prefix": title_prefix,
                "resume_session_ref": resume_session_ref,
                "orchestrator_owned": orchestrator_owned,
                "orchestrator_run_id": orchestrator_run_id,
                "granted_roots": granted_roots,
            }
        )
        return "ses_wait"


class _SequenceStore:
    def __init__(self, states: list[str]) -> None:
        self.states = states
        self.index = 0
        self.marked: list[str] = []
        self.run_ids: list[str | None] = []
        self.lookups = 0

    def mark_orchestrator_session(self, session_id: str, run_id: str | None = None) -> None:
        self.marked.append(session_id)
        self.run_ids.append(run_id)

    def get_session(self, session_id: str) -> dict[str, Any]:
        self.lookups += 1
        state = self.states[min(self.index, len(self.states) - 1)]
        self.index += 1
        return {"id": session_id, "state": state}


class _MarkerFailureStore(_SequenceStore):
    def mark_orchestrator_session(self, session_id: str, run_id: str | None = None) -> None:
        del session_id, run_id
        raise RuntimeError("database unavailable")


class _CloseFailingSupervisor(_Spawner):
    def __init__(self) -> None:
        super().__init__()
        self.dal = _SequenceStore(["completed"])
        self.close_calls = 0

    def close(
        self,
        *,
        terminate_children: bool = False,
        join_timeout: float | None = None,
    ) -> None:
        del terminate_children, join_timeout
        self.close_calls += 1
        raise SessionSupervisorCloseError("probe: ordered close timed out")


def _fake_clock() -> tuple[Callable[[], float], Callable[[float], None], list[float]]:
    now = [0.0]
    sleeps: list[float] = []

    def monotonic() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    return monotonic, sleep, sleeps


def test_runner_waits_through_nonterminal_states_before_success(tmp_path: Path) -> None:
    spawner = _Spawner()
    store = _SequenceStore(["starting", "running", "completed"])
    monotonic, sleep, sleeps = _fake_clock()
    runner = FusionSessionRunner(
        spawner=spawner,
        session_store=store,
        monotonic=monotonic,
        sleep=sleep,
        timeout_seconds=10,
        poll_interval_seconds=1,
    )

    result = runner.run(
        _request(tmp_path, ExecutorTier.FUSION),
        object(),  # type: ignore[arg-type]
    )

    assert result.status == "ok"
    assert store.marked == ["ses_wait"]
    assert store.lookups == 3
    assert sleeps == [1, 1]


def test_runner_marks_run_id_then_reports_spawn(tmp_path: Path) -> None:
    store = _SequenceStore(["completed"])
    callbacks: list[str] = []
    runner = FusionSessionRunner(
        spawner=_Spawner(),
        session_store=store,
    )
    request = _request(tmp_path, ExecutorTier.FUSION)
    request.run_id = "orch-42"

    def on_spawn(session_id: str) -> None:
        assert store.marked == [session_id]
        callbacks.append(session_id)

    request.on_spawn = on_spawn

    result = runner.run(request, object())  # type: ignore[arg-type]

    assert result.status == "ok"
    assert store.run_ids == ["orch-42"]
    assert callbacks == ["ses_wait"]


def test_spawn_callback_failure_does_not_fail_execution(tmp_path: Path) -> None:
    runner = FusionSessionRunner(
        spawner=_Spawner(),
        session_store=_SequenceStore(["completed"]),
    )
    request = _request(tmp_path, ExecutorTier.FUSION)

    def fail_callback(session_id: str) -> None:
        raise RuntimeError(f"cannot persist {session_id}")

    request.on_spawn = fail_callback

    result = runner.run(request, object())  # type: ignore[arg-type]

    assert result.status == "ok"


def test_attach_waits_for_existing_session_without_spawning(tmp_path: Path) -> None:
    spawner = _Spawner()
    store = _SequenceStore(["running", "completed"])
    monotonic, sleep, _sleeps = _fake_clock()
    runner = CheapAdapterRunner(
        spawner=spawner,
        session_store=store,
        monotonic=monotonic,
        sleep=sleep,
        timeout_seconds=2,
        poll_interval_seconds=1,
    )

    result = runner.attach("ses-existing")

    assert result.status == "ok"
    assert result.session_id == "ses-existing"
    assert spawner.calls == []
    assert store.marked == []
    assert store.lookups == 2


def test_run_returns_result_when_runner_owned_supervisor_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _CloseFailingSupervisor()
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.SessionSupervisor",
        lambda: supervisor,
    )
    runner = _MonitoredSessionRunner(default_model="sonnet")

    result = runner.run(
        _request(tmp_path, ExecutorTier.CHEAP),
        object(),  # type: ignore[arg-type]
    )

    assert result.status == "ok"
    assert result.session_id == "ses_wait"
    assert supervisor.close_calls == 1
    assert runner._owned_spawner is supervisor


def test_attach_returns_result_when_runner_owned_supervisor_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _CloseFailingSupervisor()
    monkeypatch.setattr(
        "omniagentos.sessions.supervisor.SessionSupervisor",
        lambda: supervisor,
    )
    runner = _MonitoredSessionRunner(default_model="sonnet")
    orchestrator = Orchestrator(vault_dir=str(tmp_path))
    monkeypatch.setattr(orchestrator, "_monitored_runner_for", lambda _tier: runner)

    result = orchestrator._attach(ExecutorTier.CHEAP, "ses_wait")

    assert result.status == "ok"
    assert result.session_id == "ses_wait"
    assert supervisor.close_calls == 1
    assert runner._owned_spawner is supervisor


def test_repeated_attach_reuses_and_closes_runner_owned_supervisor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated attach calls use O(1) connections and leave no supervisor pinned."""
    iterations = 25
    db_path = tmp_path / "sessions.db"
    seed_store = SessionsDal(db_path)
    seed_store.create_session(
        {
            "id": "ses_completed_attach",
            "source": "bridge",
            "project_dir": str(tmp_path),
            "provider": "claude",
            "session_ref": "completed-attach-ref",
            "state": "completed",
            "model": "sonnet",
        }
    )
    monkeypatch.setenv("OMNIAGENTOS_DB", str(db_path))
    real_connect = sqlite3.connect
    connect_calls = 0

    def counting_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        nonlocal connect_calls
        connect_calls += 1
        return real_connect(*args, **kwargs)

    with _OPEN_SUPERVISORS_LOCK:
        supervisors_before = set(_OPEN_SUPERVISORS)
    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    runner = _MonitoredSessionRunner(default_model="sonnet")
    try:
        for _ in range(iterations):
            result = runner.attach("ses_completed_attach")
            assert result.status == "ok"

        with _OPEN_SUPERVISORS_LOCK:
            owned_supervisors = set(_OPEN_SUPERVISORS) - supervisors_before
        assert connect_calls == 1, (
            f"runner opened {connect_calls} SQLite connections across {iterations} attach calls"
        )
        assert len(owned_supervisors) == 1, (
            f"runner pinned {len(owned_supervisors)} supervisors across {iterations} attach calls"
        )
        runner.close(terminate_children=False, join_timeout=0.0)

        with _OPEN_SUPERVISORS_LOCK:
            assert not (set(_OPEN_SUPERVISORS) - supervisors_before), (
                "runner-owned supervisor remained pinned after close"
            )
    finally:
        # Keep mutation/revert checks isolated even when an assertion above
        # proves the production cleanup is absent.
        with _OPEN_SUPERVISORS_LOCK:
            leaked = set(_OPEN_SUPERVISORS) - supervisors_before
        for supervisor in leaked:
            supervisor.close(terminate_children=False, join_timeout=0.0)
        seed_store.close()


@pytest.mark.parametrize("terminal_state", ["failed", "cancelled", "killed"])
def test_runner_maps_unsuccessful_terminal_states_to_error(
    tmp_path: Path, terminal_state: str
) -> None:
    runner = FusionSessionRunner(
        spawner=_Spawner(),
        session_store=_SequenceStore([terminal_state]),
        timeout_seconds=10,
    )

    result = runner.run(
        _request(tmp_path, ExecutorTier.FUSION),
        object(),  # type: ignore[arg-type]
    )

    assert result.status == "error"
    assert result.error is not None
    assert f"ended in {terminal_state}" in result.error


def test_runner_timeout_is_bounded_and_reports_last_state(tmp_path: Path) -> None:
    store = _SequenceStore(["running"])
    monotonic, sleep, sleeps = _fake_clock()
    runner = CheapAdapterRunner(
        spawner=_Spawner(),
        session_store=store,
        monotonic=monotonic,
        sleep=sleep,
        timeout_seconds=2,
        poll_interval_seconds=1,
    )

    result = runner.run(
        _request(tmp_path, ExecutorTier.CHEAP),
        object(),  # type: ignore[arg-type]
    )

    assert result.status == "error"
    assert result.error == "session ses_wait timed out after 2s in state running"
    assert sleeps == [1, 1]
    assert store.lookups == 3


class _KillRecordingStore(_SequenceStore):
    """A store that supports the A2 timeout-kill seam."""

    def __init__(self, states: list[str]) -> None:
        super().__init__(states)
        self.kill_requests: list[str] = []

    def request_kill(self, session_id: str) -> bool:
        self.kill_requests.append(session_id)
        return True


class _KillFailingStore(_KillRecordingStore):
    def request_kill(self, session_id: str) -> bool:
        super().request_kill(session_id)
        raise RuntimeError("kill unavailable")


def test_runner_timeout_requests_session_kill(tmp_path: Path) -> None:
    """A2 zombie fix: a timed-out await must request a durable kill instead of
    walking away and leaving the session running unsupervised."""
    store = _KillRecordingStore(["running"])
    monotonic, sleep, _sleeps = _fake_clock()
    runner = CheapAdapterRunner(
        spawner=_Spawner(),
        session_store=store,
        monotonic=monotonic,
        sleep=sleep,
        timeout_seconds=2,
        poll_interval_seconds=1,
    )

    result = runner.run(
        _request(tmp_path, ExecutorTier.CHEAP),
        object(),  # type: ignore[arg-type]
    )

    assert result.status == "error"
    assert store.kill_requests == ["ses_wait"]
    assert result.error == ("session ses_wait timed out after 2s in state running; kill requested")


def test_runner_timeout_kill_failure_never_masks_the_timeout(tmp_path: Path) -> None:
    """The timeout result is authoritative; a failing request_kill is logged only."""
    store = _KillFailingStore(["running"])
    monotonic, sleep, _sleeps = _fake_clock()
    runner = CheapAdapterRunner(
        spawner=_Spawner(),
        session_store=store,
        monotonic=monotonic,
        sleep=sleep,
        timeout_seconds=2,
        poll_interval_seconds=1,
    )

    result = runner.run(
        _request(tmp_path, ExecutorTier.CHEAP),
        object(),  # type: ignore[arg-type]
    )

    assert result.status == "error"
    assert store.kill_requests == ["ses_wait"]  # it was attempted
    assert result.error == "session ses_wait timed out after 2s in state running"


def test_ownership_marker_failure_still_waits_and_never_reports_success(
    tmp_path: Path,
) -> None:
    store = _MarkerFailureStore(["running", "completed"])
    monotonic, sleep, _sleeps = _fake_clock()
    runner = FusionSessionRunner(
        spawner=_Spawner(),
        session_store=store,
        monotonic=monotonic,
        sleep=sleep,
        timeout_seconds=2,
        poll_interval_seconds=1,
    )

    callbacks: list[str] = []
    request = _request(tmp_path, ExecutorTier.FUSION)
    request.on_spawn = callbacks.append
    result = runner.run(request, object())  # type: ignore[arg-type]

    assert store.lookups == 2
    assert result.status == "error"
    assert result.error is not None
    assert "could not mark session ses_wait orchestrator-owned" in result.error
    assert callbacks == []


def test_runner_forwards_request_granted_roots_to_spawn(tmp_path: Path) -> None:
    """FIX 6: the monitored runner freezes the request's granted roots onto the spawned
    session, so an orchestrate-mode executor honors the project's full scope."""
    spawner = _Spawner()
    runner = FusionSessionRunner(
        spawner=spawner,
        session_store=_SequenceStore(["completed"]),
    )
    request = _request(tmp_path, ExecutorTier.FUSION)
    request.granted_roots = ["/srv/repo2", "/srv/shared"]

    result = runner.run(request, object())  # type: ignore[arg-type]

    assert result.status == "ok"
    assert spawner.calls[0]["granted_roots"] == ["/srv/repo2", "/srv/shared"]


def test_runner_passes_none_granted_roots_when_request_has_none(tmp_path: Path) -> None:
    """Invariant: a request with no granted roots spawns with granted_roots=None --
    byte-identical to pre-P3 (working-dir-only) confinement."""
    spawner = _Spawner()
    runner = FusionSessionRunner(
        spawner=spawner,
        session_store=_SequenceStore(["completed"]),
    )

    runner.run(_request(tmp_path, ExecutorTier.FUSION), object())  # type: ignore[arg-type]

    assert spawner.calls[0]["granted_roots"] is None


def test_cheap_runner_honours_an_explicit_model_pin(tmp_path: Path) -> None:
    spawner = _Spawner()
    runner = CheapAdapterRunner(
        spawner=spawner,
        session_store=_SequenceStore(["completed"]),
    )

    result = runner.run(
        _request(tmp_path, ExecutorTier.CHEAP, model="haiku"),
        object(),  # type: ignore[arg-type]
    )

    assert result.status == "ok"
    assert spawner.calls[0]["model"] == "haiku"
