from __future__ import annotations

import io
import json
import queue
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import omniagentos.runner.sandbox as runner_sandbox
import omniagentos.swarm.provider_exec as provider_exec_module
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.orchestrator.contracts import ExecutorResult
from omniagentos.sessions.dal import SessionsDal
from omniagentos.swarm.provider_exec import (
    PROVIDER_CONFIG_ENV,
    ProviderExecPolicyError,
    ProviderSessionRunner,
    classify_provider_doctor_outcome,
)
from tests.support.db_template import migrated_db

_EOF = object()


@pytest.mark.parametrize(
    ("provider", "status", "expected"),
    [
        ("grok", {"ok": True}, "ok"),
        (
            "grok",
            {"ok": False, "error": "HTTP 401: invalid API key"},
            "auth_error",
        ),
        (
            "grok",
            {
                "ok": False,
                "probe_status": "error",
                "probe_error": "session ses_a401 ended in failed: invalid api key",
            },
            "auth_error",
        ),
        (
            "gemini",
            {"ok": False, "probe_status": "error", "probe_error": "process exited 1"},
            "harness_error",
        ),
        (
            "grok",
            {"ok": False, "probe_status": "error", "probe_error": "out of credits"},
            "quota_exhausted",
        ),
        (
            "gemini",
            {"ok": False, "probe_status": "error", "probe_error": "resource_exhausted"},
            "transient_rate_limit",
        ),
        ("grok", {"ok": True, "probe_status": "error"}, "harness_error"),
        ("kimi", {"ok": False}, "unavailable"),
    ],
)
def test_classify_provider_doctor_outcome_never_defaults_to_ok(
    provider: str, status: dict[str, Any], expected: str
) -> None:
    """Pure classification only: provider_doctor itself remains live-only."""
    assert classify_provider_doctor_outcome(provider, status) == expected


class _DoctorDal:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.killed: set[str] = set()

    def request_kill(self, session_id: str, *, killed_by: str | None = None) -> bool:
        del killed_by
        self.killed.add(session_id)
        return True

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        if session_id in self.killed:
            return {"state": "killed"}
        return self.row


@pytest.mark.parametrize(
    ("probe_result", "expected_outcome"),
    [
        (ExecutorResult(status="ok", session_id="probe"), "ok"),
        (
            ExecutorResult(
                status="error",
                session_id="probe",
                error="api_key=supersecretvalue " + "x" * 600,
            ),
            "harness_error",
        ),
    ],
)
def test_provider_doctor_persists_scrubbed_probe_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    probe_result: ExecutorResult,
    expected_outcome: str,
) -> None:
    dal = _DoctorDal()
    runner = ProviderSessionRunner(
        dal=dal,  # type: ignore[arg-type]
        db_path=str(tmp_path / "unused.sqlite3"),
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
        reserve_account=lambda *args, **kwargs: None,
        convert_reservation=lambda *args, **kwargs: True,
        report_outcome=lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(runner, "_enabled_accounts", lambda _provider: [])
    session_ids = iter(("probe", "kill"))
    monkeypatch.setattr(runner, "spawn", lambda *args, **kwargs: next(session_ids))
    terminal_results = iter(
        (
            probe_result,
            ExecutorResult(status="error", session_id="kill", error="killed by doctor"),
        )
    )
    monkeypatch.setattr(
        runner,
        "await_terminal",
        lambda _session_id, _timeout, *, poll: next(terminal_results),
    )
    runner._stream_counts.update({"probe": 1, "kill": 1})

    status = runner.provider_doctor(["grok"])["grok:default"]

    assert status["probe_status"] == probe_result.status
    assert status["outcome"] == expected_outcome
    if probe_result.error is None:
        assert status["probe_error"] is None
    else:
        assert "supersecretvalue" not in status["probe_error"]
        assert "[REDACTED]" in status["probe_error"]
        assert len(status["probe_error"]) == 500


@pytest.mark.parametrize("branch", ["killed", "invalid", "timeout"])
def test_await_terminal_harness_session_id_is_not_auth_error(branch: str) -> None:
    session_id = "ses_a401_harness"
    if branch == "killed":
        dal = _DoctorDal({"state": "killed", "error": None})
        ticks = iter((0.0,))
    elif branch == "invalid":
        dal = _DoctorDal({"state": "not-a-state"})
        ticks = iter((0.0,))
    else:
        dal = _DoctorDal({"state": "running"})
        ticks = iter((0.0, 1.0))

    def monotonic() -> float:
        return next(ticks)

    runner = ProviderSessionRunner(
        dal=dal,  # type: ignore[arg-type]
        monotonic=monotonic,
        sleep=lambda _seconds: None,
        reserve_account=lambda *args, **kwargs: None,
        convert_reservation=lambda *args, **kwargs: True,
        report_outcome=lambda *args, **kwargs: None,
    )

    result = runner.await_terminal(session_id, 0.5, poll=0.1)
    outcome = classify_provider_doctor_outcome(
        "grok",
        {"ok": False, "probe_status": result.status, "probe_error": result.error},
    )

    assert outcome == "harness_error"


@pytest.fixture(autouse=True)
def _sandbox_wrap_available_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep process-lifecycle tests independent of host sandbox discovery."""
    monkeypatch.setattr(runner_sandbox, "sandbox_available", lambda: True)
    monkeypatch.setattr(runner_sandbox, "wrap_available", lambda _argv, _workspace_dir: True)


class _QueueStream:
    def __init__(self, lines: list[str]) -> None:
        self._items: queue.Queue[Any] = queue.Queue()
        for line in lines:
            self._items.put(line)

    def finish(self) -> None:
        self._items.put(_EOF)

    def __iter__(self) -> _QueueStream:
        return self

    def __next__(self) -> str:
        item = self._items.get(timeout=2)
        if item is _EOF:
            raise StopIteration
        return str(item)


class _FakeProcess:
    next_pid = 7000

    def __init__(self, *, lines: list[str], returncode: int, hang: bool) -> None:
        self.pid = _FakeProcess.next_pid
        _FakeProcess.next_pid += 1
        self.stdout = _QueueStream(lines)
        self.stdin = io.StringIO()
        self.returncode: int | None = None if hang else returncode
        self._exit_code = returncode
        if not hang:
            self.stdout.finish()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode if self.returncode is not None else self._exit_code

    def stop(self, sig: int) -> None:
        self.returncode = -sig
        self.stdout.finish()


class _FakeAdapter:
    def __init__(self, provider: str) -> None:
        self.name = provider
        self.cli = provider

    def _command(self, input: Any, prompt: str, session_ref: str | None) -> list[str]:
        del input, session_ref
        return [self.cli, "--prompt", prompt]

    def _sandboxed_launch(
        self,
        command: list[str],
        working_dir: str,
        extra_write_roots: list[str] | None = None,
        **kwargs: Any,
    ) -> list[str]:
        del working_dir, extra_write_roots, kwargs
        return command

    def _parse(self, stdout: str) -> Any:
        decoded = json.loads(stdout)
        return SimpleNamespace(
            text=str(decoded["text"]),
            session_ref=decoded.get("session_ref"),
        )


@pytest.fixture
def harness(
    tmp_path: Path,
) -> tuple[ProviderSessionRunner, SessionsDal, list[dict[str, Any]], list[_FakeProcess]]:
    db = str(tmp_path / "provider-exec.db")
    db = migrated_db(CollabStore, db)
    dal = SessionsDal(db)
    scenarios: list[dict[str, Any]] = []
    processes: list[_FakeProcess] = []
    captures: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    # Incremented at factory entry (before scenario pop) so zero-spawn tests
    # still count invocations when the factory raises or scenarios are empty.
    process_factory_calls: list[int] = [0]

    def process_factory(command: list[str], **kwargs: Any) -> _FakeProcess:
        process_factory_calls[0] += 1
        scenario = scenarios.pop(0)
        process = _FakeProcess(**scenario)
        processes.append(process)
        captures.append({"command": command, **kwargs})
        return process

    def killpg(pgid: int, sig: int) -> None:
        for process in processes:
            if process.pid == pgid:
                process.stop(sig)
                return
        raise ProcessLookupError(pgid)

    def report(provider: str, account_id: str, outcome: str, detail: str, **kwargs: Any) -> None:
        outcomes.append(
            {
                "provider": provider,
                "account_id": account_id,
                "outcome": outcome,
                "detail": detail,
                **kwargs,
            }
        )

    runner = ProviderSessionRunner(
        dal,
        db_path=db,
        process_factory=process_factory,
        adapter_resolver=lambda provider: _FakeAdapter(provider),
        poll_interval=0.01,
        kill_grace=0.01,
        getpgid=lambda pid: pid,
        killpg=killpg,
        pgid_liveness=lambda pid: any(
            process.pid == pid and process.poll() is None for process in processes
        ),
        command_reader=lambda pid: "",
        reserve_account=lambda *args, **kwargs: None,
        convert_reservation=lambda *args, **kwargs: True,
        report_outcome=report,
    )
    runner._test_scenarios = scenarios  # type: ignore[attr-defined]
    runner._test_captures = captures  # type: ignore[attr-defined]
    runner._test_outcomes = outcomes  # type: ignore[attr-defined]
    runner._test_process_factory_calls = process_factory_calls  # type: ignore[attr-defined]
    return runner, dal, captures, processes


def _spawn(
    runner: ProviderSessionRunner,
    working_dir: Path,
    *,
    provider: str = "codex",
    account_id: str | None = "acct_test",
    wall_timeout_seconds: float | None = None,
) -> str:
    return runner.spawn(
        provider,
        "model-test",
        "do the task",
        str(working_dir),
        "btk_owned",
        "swr_test",
        1.0,
        7,
        "none",
        account_id,
        wall_timeout_seconds,
    )


@pytest.mark.parametrize("risk", ["external", "deploy", "destructive"])
def test_risk_class_hard_deny_creates_no_row(
    harness: tuple[ProviderSessionRunner, SessionsDal, list[dict[str, Any]], list[_FakeProcess]],
    tmp_path: Path,
    risk: str,
) -> None:
    runner, dal, _, _ = harness
    with pytest.raises(ProviderExecPolicyError):
        runner.spawn(
            "gemini",
            "model",
            "prompt",
            str(tmp_path),
            "btk",
            "swr",
            None,
            5,
            risk,
        )
    assert dal.list_sessions() == []


def test_row_lifecycle_marker_output_and_resume_ref(
    harness: tuple[ProviderSessionRunner, SessionsDal, list[dict[str, Any]], list[_FakeProcess]],
    tmp_path: Path,
) -> None:
    runner, dal, _, _ = harness
    runner._test_scenarios.append(  # type: ignore[attr-defined]
        {
            "lines": ['{"text":"finished","session_ref":"resume-123"}\n'],
            "returncode": 0,
            "hang": False,
        }
    )
    session_id = _spawn(runner, tmp_path)
    result = runner.await_terminal(session_id, 2, poll=0.01)
    row = dal.get_session(session_id)

    assert result.status == "ok"
    assert result.output_text == "finished"
    assert row is not None
    assert row["source"] == "bridge"
    assert row["provider"] == "codex"
    assert row["account_id"] == "acct_test"
    assert row["state"] == "completed"
    assert row["session_ref"] == "resume-123"
    assert row["output_text"] == "finished"
    assert row["idle_minutes"] == 7
    assert "[swarm:btk_owned]" in row["title"]
    assert isinstance(row["pid"], int)


def test_spawned_session_cost_starts_unknown_not_free(
    harness: tuple[ProviderSessionRunner, SessionsDal, list[dict[str, Any]], list[_FakeProcess]],
    tmp_path: Path,
) -> None:
    """Before a CLI reports a price, the live session must carry NULL, never $0."""
    runner, dal, _, _ = harness
    runner._test_scenarios.append(  # type: ignore[attr-defined]
        {"lines": ["started\n"], "returncode": 0, "hang": True}
    )

    session_id = _spawn(runner, tmp_path, provider="codex")
    row = dal.get_session(session_id)

    assert row is not None
    assert row["state"] == "running"
    assert row["cost_usd"] is None
    assert row["cost_usd"] != 0.0

    assert dal.request_kill(session_id, killed_by="test-cleanup")
    runner.await_terminal(session_id, 2, poll=0.01)


def test_nonzero_exit_terminalizes_failed(
    harness: tuple[ProviderSessionRunner, SessionsDal, list[dict[str, Any]], list[_FakeProcess]],
    tmp_path: Path,
) -> None:
    runner, dal, _, _ = harness
    runner._test_scenarios.append(  # type: ignore[attr-defined]
        {"lines": ["ordinary failure\n"], "returncode": 9, "hang": False}
    )
    session_id = _spawn(runner, tmp_path, account_id=None)
    result = runner.await_terminal(session_id, 2, poll=0.01)
    row = dal.get_session(session_id)
    assert result.status == "error"
    assert row is not None and row["state"] == "failed"
    assert "ordinary failure" in str(row["error"])


def test_kill_requested_terminates_group_and_terminalizes_killed(
    harness: tuple[ProviderSessionRunner, SessionsDal, list[dict[str, Any]], list[_FakeProcess]],
    tmp_path: Path,
) -> None:
    runner, dal, _, processes = harness
    runner._test_scenarios.append(  # type: ignore[attr-defined]
        {"lines": ["started\n"], "returncode": 0, "hang": True}
    )
    session_id = _spawn(runner, tmp_path)
    assert dal.request_kill(session_id, killed_by="test-operator")
    result = runner.await_terminal(session_id, 2, poll=0.01)
    row = dal.get_session(session_id)
    assert result.status == "error"
    assert row is not None and row["state"] == "killed"
    assert row["killed_by"] == "test-operator"
    assert processes[0].returncode == -signal.SIGTERM


def test_kill_requested_but_unconfirmed_death_stays_non_terminal(
    tmp_path: Path,
) -> None:
    """F001 (round-3 repair, receipt 5283694621): a kill signal whose death is
    never confirmed must not terminalize the row as KILLED -- that would
    report a confirmed kill for a process the runner never proved dead. The
    row must stay non-terminal (kill_requested still set) so a later cancel
    read-back reports kill_pending / kill_complete=false, fail closed.
    """
    db = str(tmp_path / "provider-exec-unconfirmed.db")
    db = migrated_db(CollabStore, db)
    dal = SessionsDal(db)
    scenarios: list[dict[str, Any]] = []
    processes: list[_FakeProcess] = []

    def process_factory(command: list[str], **kwargs: Any) -> _FakeProcess:
        del command, kwargs
        scenario = scenarios.pop(0)
        process = _FakeProcess(**scenario)
        processes.append(process)
        return process

    def killpg_swallows_failure(pgid: int, sig: int) -> None:
        # The production `_signal_group` swallows ProcessLookupError /
        # PermissionError / OSError -- simulate that here by delivering the
        # signal to nothing: the process never actually dies, `poll()` keeps
        # returning None, but no exception propagates to the caller.
        del pgid, sig

    runner = ProviderSessionRunner(
        dal,
        db_path=db,
        process_factory=process_factory,
        adapter_resolver=lambda provider: _FakeAdapter(provider),
        poll_interval=0.01,
        kill_grace=0.01,
        getpgid=lambda pid: pid,
        killpg=killpg_swallows_failure,
        pgid_liveness=lambda pid: any(
            process.pid == pid and process.poll() is None for process in processes
        ),
        command_reader=lambda pid: "",
        reserve_account=lambda *args, **kwargs: None,
        convert_reservation=lambda *args, **kwargs: True,
        report_outcome=lambda *args, **kwargs: None,
    )
    scenarios.append({"lines": ["started\n"], "returncode": 0, "hang": True})

    session_id = _spawn(runner, tmp_path)
    assert dal.request_kill(session_id, killed_by="test-operator")

    # The reader thread should observe kill_requested, attempt to terminate,
    # fail to confirm death, and stop -- the row never reaches a terminal
    # state, so await_terminal's own deadline fires and reports a timeout
    # (not a terminal state read back).
    result = runner.await_terminal(session_id, 0.5, poll=0.01)
    assert result.status == "error"
    assert "timed out" in str(result.error)

    row = dal.get_session(session_id)
    assert row is not None
    assert row["state"] == "running", (
        "an unconfirmed group kill terminalized the session row",
        row,
    )
    assert bool(row["kill_requested"]) is True


def test_wall_timeout_kills_group_and_fails_with_timeout(
    harness: tuple[ProviderSessionRunner, SessionsDal, list[dict[str, Any]], list[_FakeProcess]],
    tmp_path: Path,
) -> None:
    runner, dal, _, processes = harness
    runner._test_scenarios.append(  # type: ignore[attr-defined]
        {"lines": [], "returncode": 0, "hang": True}
    )
    session_id = _spawn(runner, tmp_path, wall_timeout_seconds=0.05)
    runner.await_terminal(session_id, 2, poll=0.01)
    row = dal.get_session(session_id)
    assert row is not None and row["state"] == "failed"
    assert "timed out" in str(row["error"])
    assert row["killed_by"] == "timeout"
    assert processes[0].returncode == -signal.SIGTERM


def test_process_command_requests_untruncated_output(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []
    command = "/usr/bin/sandbox-exec " + "x" * 320 + " /usr/local/bin/grok"

    def fake_run(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        captured.extend(argv)
        return SimpleNamespace(returncode=0, stdout=command + "\n")

    monkeypatch.setattr(provider_exec_module.subprocess, "run", fake_run)

    observed = provider_exec_module._process_command(4242)

    assert captured == ["ps", "-ww", "-o", "command=", "-p", "4242"]
    assert observed.endswith("/usr/local/bin/grok")


def test_reconcile_same_pid_different_command_is_crashed(
    harness: tuple[ProviderSessionRunner, SessionsDal, list[dict[str, Any]], list[_FakeProcess]],
    tmp_path: Path,
) -> None:
    runner, dal, _, _ = harness
    session_id = new_id("ses")
    now = utc_now_iso()
    dal.create_session(
        {
            "id": session_id,
            "source": "bridge",
            "project_dir": str(tmp_path),
            "provider": "grok",
            "state": "running",
            "pid": 4242,
            "model": "grok-model",
            "title": "[swarm:btk] grok",
            "last_activity_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    runner._pgid_liveness = lambda pid: pid == 4242
    runner._command_reader = lambda pid: "/usr/bin/python unrelated_worker.py"
    assert runner.reconcile_orphans()[session_id] == "crashed"
    row = dal.get_session(session_id)
    assert row is not None and row["state"] == "failed"
    assert "identity changed" in str(row["error"])


def test_failed_rate_limit_reports_to_limit_state(
    harness: tuple[ProviderSessionRunner, SessionsDal, list[dict[str, Any]], list[_FakeProcess]],
    tmp_path: Path,
) -> None:
    runner, _, _, _ = harness
    runner._test_scenarios.append(  # type: ignore[attr-defined]
        {
            "lines": ["429 too many requests: rate limit reached\n"],
            "returncode": 1,
            "hang": False,
        }
    )
    session_id = _spawn(runner, tmp_path, provider="grok")
    runner.await_terminal(session_id, 2, poll=0.01)
    assert runner._test_outcomes[0]["outcome"] == "transient_rate_limit"  # type: ignore[attr-defined]
    assert runner._test_outcomes[0]["provider"] == "grok"  # type: ignore[attr-defined]
    assert runner._test_outcomes[0]["account_id"] == "acct_test"  # type: ignore[attr-defined]


@pytest.mark.parametrize("provider", sorted(PROVIDER_CONFIG_ENV))
def test_env_has_exactly_one_provider_config_var(
    harness: tuple[ProviderSessionRunner, SessionsDal, list[dict[str, Any]], list[_FakeProcess]],
    tmp_path: Path,
    provider: str,
) -> None:
    runner, _, captures, _ = harness
    runner._test_scenarios.append(  # type: ignore[attr-defined]
        {
            "lines": ['{"text":"ok","session_ref":null}\n'],
            "returncode": 0,
            "hang": False,
        }
    )
    session_id = _spawn(runner, tmp_path, provider=provider)
    runner.await_terminal(session_id, 2, poll=0.01)
    env = captures[-1]["env"]
    present = set(env) & set(PROVIDER_CONFIG_ENV.values())
    assert present == {PROVIDER_CONFIG_ENV[provider]}
    allowed = {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "TERM",
        "OMNIAGENTOS_BRIDGE_SESSION_ID",
        PROVIDER_CONFIG_ENV[provider],
    }
    # Gemini headless needs workspace trust + optional API key forward.
    if provider == "gemini":
        allowed |= {
            "GEMINI_CLI_TRUST_WORKSPACE",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
        }
        assert env.get("GEMINI_CLI_TRUST_WORKSPACE") == "true"
    assert set(env) <= allowed


def test_await_timeout_requests_kill_before_returning(
    harness: tuple[ProviderSessionRunner, SessionsDal, list[dict[str, Any]], list[_FakeProcess]],
    tmp_path: Path,
) -> None:
    runner, dal, _, _ = harness
    runner._test_scenarios.append(  # type: ignore[attr-defined]
        {"lines": [], "returncode": 0, "hang": True}
    )
    session_id = _spawn(runner, tmp_path)
    result = runner.await_terminal(session_id, 0.02, poll=0.005)
    assert result.status == "error"
    assert "kill requested" in str(result.error)
    assert bool((dal.get_session(session_id) or {})["kill_requested"])


def test_provider_sandbox_opens_only_selected_provider_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omniagentos.runner import sandbox

    monkeypatch.setenv("HOME", str(tmp_path))
    custom = tmp_path / "codex-account"
    roots = set(sandbox.adapter_write_roots("codex", str(custom)))
    assert str(tmp_path / ".codex") in roots
    assert str(custom) in roots
    assert str(tmp_path / ".gemini") not in roots
    assert str(tmp_path / ".grok") not in roots
    assert str(tmp_path / ".kimi-code") not in roots
    assert str(tmp_path / ".qwen") not in roots


class TestInnerSandboxRewrite:
    def test_codex_sandbox_rewritten_outer_only(self) -> None:
        # provider_exec delegates to the shared adapters.common helper.
        from omniagentos.swarm.provider_exec import _disable_inner_sandbox

        cmd = ["codex", "exec", "--sandbox", "workspace-write", "-C", "/w", "-"]
        rewritten = _disable_inner_sandbox(cmd, "codex")
        assert rewritten[rewritten.index("--sandbox") + 1] == "danger-full-access"
        assert rewritten[:2] == ["codex", "exec"] and rewritten[-3:] == ["-C", "/w", "-"]

    def test_grok_sandbox_pair_removed_entirely(self) -> None:
        # D2: grok rejects codex vocabulary ('danger-full-access' refused at
        # startup, live) — under the outer wrap its --sandbox pair is REMOVED
        # so the CLI runs at its documented default (off).
        from omniagentos.swarm.provider_exec import _disable_inner_sandbox

        gcmd = ["grok", "-p", "x", "--sandbox", "workspace", "--cwd", "/w"]
        assert _disable_inner_sandbox(gcmd, "grok") == ["grok", "-p", "x", "--cwd", "/w"]
        gcmd_ro = ["grok", "-p", "x", "--sandbox", "read-only"]
        assert _disable_inner_sandbox(gcmd_ro, "grok") == ["grok", "-p", "x"]

    def test_non_self_sandboxing_providers_untouched(self) -> None:
        from omniagentos.swarm.provider_exec import _disable_inner_sandbox

        cmd = ["kimi", "--prompt", "x"]
        assert _disable_inner_sandbox(cmd, "kimi") == cmd
        assert _disable_inner_sandbox(cmd, "gemini") == cmd

    def test_shared_helper_is_the_adapters_common_one(self) -> None:
        from omniagentos.adapters.common import disable_inner_sandbox
        from omniagentos.swarm.provider_exec import _disable_inner_sandbox

        assert _disable_inner_sandbox is disable_inner_sandbox


class _SelfSandboxingFakeAdapter(_FakeAdapter):
    """Fake adapter whose argv carries the CLI's OWN --sandbox pair, mirroring
    the real codex/grok command shapes provider_exec spawns."""

    def _command(self, input: Any, prompt: str, session_ref: str | None) -> list[str]:
        del input, session_ref
        if self.name == "codex":
            return ["codex", "exec", "--sandbox", "workspace-write", "--prompt", prompt]
        return ["grok", "-p", prompt, "--sandbox", "workspace"]


class TestSpawnInnerSandboxWrapGating:
    """spawn()'s inner-sandbox disable is gated on runner.sandbox.wrap_available
    (the SAME predicate CliAdapter._invoke gates on): an unproven outer
    Seatbelt wrap refuses the unattended launch before process creation."""

    def _spawn_and_capture(
        self,
        harness: tuple[
            ProviderSessionRunner, SessionsDal, list[dict[str, Any]], list[_FakeProcess]
        ],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        provider: str,
        wrap_available: bool,
    ) -> list[str]:
        runner, _, captures, _ = harness
        runner._adapter_resolver = lambda p: _SelfSandboxingFakeAdapter(p)
        monkeypatch.setattr(runner_sandbox, "sandbox_available", lambda: bool(wrap_available))
        monkeypatch.setattr(
            runner_sandbox,
            "wrap_available",
            lambda argv, workspace_dir: wrap_available,
        )
        runner._test_scenarios.append(  # type: ignore[attr-defined]
            {
                "lines": ['{"text":"ok","session_ref":null}\n'],
                "returncode": 0,
                "hang": False,
            }
        )
        session_id = _spawn(runner, tmp_path, provider=provider)
        runner.await_terminal(session_id, 2, poll=0.01)
        return list(captures[-1]["command"])

    def test_codex_rewritten_when_wrap_engages(
        self, harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        command = self._spawn_and_capture(
            harness, tmp_path, monkeypatch, provider="codex", wrap_available=True
        )
        assert command[command.index("--sandbox") + 1] == "danger-full-access"
        assert "workspace-write" not in command

    @pytest.mark.parametrize("provider", ["codex", "grok"])
    def test_wrap_unavailable_refuses_before_process_creation(
        self,
        harness,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        provider: str,
    ) -> None:
        runner, dal, captures, processes = harness
        runner._adapter_resolver = lambda p: _SelfSandboxingFakeAdapter(p)
        # Patch the module object so spawn()'s local import always sees the
        # fail-closed predicates (string-path patches can miss under concurrent
        # collection / reimport).
        monkeypatch.setattr(runner_sandbox, "sandbox_available", lambda: False)
        monkeypatch.setattr(runner_sandbox, "wrap_available", lambda _argv, _workspace_dir: False)

        spawn_error: BaseException | None = None
        try:
            _spawn(runner, tmp_path, provider=provider)
        except BaseException as exc:
            spawn_error = exc

        # Durable zero-spawn proof: counter increments at factory ENTRY (before
        # scenario pop / captures append). Always assert this first — captures
        # and processes stay empty if pop fails, so they alone are not a spawn
        # invocation counter.
        factory_calls = runner._test_process_factory_calls[0]  # type: ignore[attr-defined]
        assert factory_calls == 0, (
            f"unattended launch must not invoke process_factory when sandbox "
            f"unproven; process_factory_calls={factory_calls}"
        )
        assert captures == []
        assert processes == []
        assert spawn_error is not None
        assert isinstance(spawn_error, RuntimeError)
        assert "sandbox" in str(spawn_error).lower() and "refus" in str(spawn_error).lower()
        session_id = getattr(spawn_error, "session_id", None)
        assert session_id is not None
        row = dal.get_session(session_id)
        assert row is not None and row["state"] == "failed"

    def test_grok_pair_removed_when_wrap_engages(
        self, harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        command = self._spawn_and_capture(
            harness, tmp_path, monkeypatch, provider="grok", wrap_available=True
        )
        assert "--sandbox" not in command
        assert "workspace" not in command


def test_gemini_clean_exit_with_stderr_noise_terminalizes_completed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """END-TO-END regression for the clean-exit-parsed-as-failure bug: the REAL
    GeminiAdapter (not _FakeAdapter) parses a merged noise+JSON stream from a
    rc=0 fake process, and the session must terminalize COMPLETED — not FAILED
    via the parse-exception path in _finish_process."""
    from omniagentos.adapters.gemini import GeminiAdapter

    # Keep the launch argv construction offline/deterministic.
    monkeypatch.setattr(
        "omniagentos.runner.sandbox.wrap_command",
        lambda command, workspace_dir, **kwargs: command,
    )
    db = str(tmp_path / "provider-exec-gemini.db")
    db = migrated_db(CollabStore, db)
    dal = SessionsDal(db)
    scenarios: list[dict[str, Any]] = []
    processes: list[_FakeProcess] = []

    def process_factory(command: list[str], **kwargs: Any) -> _FakeProcess:
        del command, kwargs
        process = _FakeProcess(**scenarios.pop(0))
        processes.append(process)
        return process

    runner = ProviderSessionRunner(
        dal,
        db_path=db,
        process_factory=process_factory,
        adapter_resolver=lambda provider: GeminiAdapter(),
        poll_interval=0.01,
        kill_grace=0.01,
        getpgid=lambda pid: pid,
        killpg=lambda pgid, sig: None,
        pgid_liveness=lambda pid: False,
        command_reader=lambda pid: "",
        reserve_account=lambda *args, **kwargs: None,
        convert_reservation=lambda *args, **kwargs: True,
        report_outcome=lambda *args, **kwargs: None,
    )
    envelope = json.dumps(
        {
            "session_id": "b78ebd83-1111",
            "response": "done cleanly",
            "stats": {"input_tokens": 10, "output_tokens": 5},
        },
        indent=2,
    )
    scenarios.append(
        {
            "lines": [
                "Loaded cached credentials.\n",
                "EPERM: operation not permitted, open "
                "'/var/folders/zz/T/gemini-client-error.json'\n",
                *[line + "\n" for line in envelope.splitlines()],
            ],
            "returncode": 0,
            "hang": False,
        }
    )
    session_id = _spawn(runner, tmp_path, provider="gemini", account_id=None)
    result = runner.await_terminal(session_id, 2, poll=0.01)
    row = dal.get_session(session_id)
    assert result.status == "ok"
    assert row is not None
    assert row["state"] == "completed"
    assert row["output_text"] == "done cleanly"
    assert row["session_ref"] == "b78ebd83-1111"


def test_extra_write_roots_thread_into_sandboxed_launch(
    harness: tuple[ProviderSessionRunner, SessionsDal, list[dict[str, Any]], list[_FakeProcess]],
    tmp_path: Path,
) -> None:
    """Merge-model Phase 2: the git common dir rides ``extra_write_roots``
    into the adapter's outer Seatbelt profile, AFTER the provider state dir."""
    runner, _, _, _ = harness
    recorded: dict[str, Any] = {}

    class CapturingAdapter(_FakeAdapter):
        def _sandboxed_launch(
            self,
            command: list[str],
            working_dir: str,
            extra_write_roots: list[str] | None = None,
            **kwargs: Any,
        ) -> list[str]:
            recorded["roots"] = list(extra_write_roots or [])
            return command

    runner._adapter_resolver = lambda provider: CapturingAdapter(provider)  # type: ignore[method-assign]
    runner._test_scenarios.append(  # type: ignore[attr-defined]
        {"lines": ['{"text":"ok"}\n'], "returncode": 0, "hang": False}
    )
    session_id = runner.spawn(
        "codex",
        "model-test",
        "do the task",
        str(tmp_path),
        "btk_owned",
        "swr_test",
        1.0,
        7,
        "none",
        "acct_test",
        extra_write_roots=["/main-repo/.git"],
    )
    assert session_id
    # config state dir first (existing contract), then the threaded extras.
    assert recorded["roots"][1:] == ["/main-repo/.git"]
    runner.await_terminal(session_id, 2, poll=0.01)
