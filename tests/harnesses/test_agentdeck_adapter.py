"""Tests for AgentDeckAdapter (omniagentos/harnesses/agentdeck/adapter.py).

Verifies launch commands, health indicators, subprocess polling loops,
scoping rules, and tmux capture integration.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omniagentos.contracts import (
    AgentAdapter,
    AgentInput,
    HarnessProfile,
    HarnessType,
    HealthStatus,
    ResultStatus,
)
from omniagentos.harnesses.agentdeck import adapter as adapter_module
from omniagentos.harnesses.agentdeck.adapter import AgentDeckAdapter


@pytest.fixture(autouse=True)
def mock_detect_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock detecting binary to make tests environment-independent."""
    monkeypatch.setattr(adapter_module, "_detect_binary", lambda: "/mock/bin/agent-deck")


def test_isinstance_agent_adapter() -> None:
    assert isinstance(AgentDeckAdapter(), AgentAdapter)
    assert AgentDeckAdapter.name == "agentdeck"


def test_profile_returns_harness_profile() -> None:
    profile = AgentDeckAdapter().profile()
    assert isinstance(profile, HarnessProfile)
    assert profile.harness == HarnessType.AGENTDECK
    assert profile.version == "1.10.10"  # mocked version fallback


def test_health_healthy_when_binary_responsive(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class MockCompletedProcess:
        returncode = 0
        stdout = ""
        stderr = ""

    def mock_run(args: list[str], **kwargs: Any) -> Any:
        calls.append(args)
        return MockCompletedProcess()

    monkeypatch.setattr(adapter_module.subprocess, "run", mock_run)

    result = AgentDeckAdapter().health()
    assert isinstance(result, HealthStatus)
    assert result.healthy is True
    assert result.capabilities == {"live_runs": True}
    assert "responsive" in result.detail
    assert ["/mock/bin/agent-deck", "status", "-q"] in calls


def test_health_unhealthy_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter_module, "_detect_binary", lambda: None)

    result = AgentDeckAdapter().health()
    assert result.healthy is False
    assert result.capabilities == {"live_runs": False}
    assert "binary not found" in result.detail


def test_health_unhealthy_when_binary_crashes(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def mock_run_fail(args: list[str], **kwargs: Any) -> Any:
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(adapter_module.subprocess, "run", mock_run_fail)

    result = AgentDeckAdapter().health()
    assert result.healthy is False
    assert result.capabilities == {"live_runs": False}
    assert "status check failed" in result.detail


def test_run_boundary_rule_rejects_swarm_sessions() -> None:
    """Boundary Rule: Adapter must refuse swarm-owned sessions with a clean error."""
    adapter = AgentDeckAdapter()

    # Refuse based on run_id prefix
    res1 = adapter.run(AgentInput(run_id="swarm:123", task_id="tsk-123", prompt="say hi"))
    assert res1.status == ResultStatus.ERROR
    assert "refuses swarm scheduler tasks" in (res1.error or "")

    # Refuse based on prompt content
    res2 = adapter.run(
        AgentInput(run_id="user-run", task_id="tsk-123", prompt="[swarm:456] do task")
    )
    assert res2.status == ResultStatus.ERROR
    assert "refuses swarm scheduler tasks" in (res2.error or "")


def test_run_launches_polls_and_captures_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify standard run executes the subprocess flow, polls JSON status, and captures pane."""
    run_id = "test-adapter-run"
    prompt = "explain mitosis"

    class MockCompletedProcess:
        def __init__(self, returncode: int, stdout: str) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    calls: list[list[str]] = []
    poll_count = 0

    def mock_run(args: list[str], **kwargs: Any) -> Any:
        nonlocal poll_count
        calls.append(args)

        # 1. Mock launch command
        if "launch" in args:
            assert "-t" in args
            idx = args.index("-t")
            assert args[idx + 1] == run_id
            return MockCompletedProcess(0, "launched session test-adapter-run")

        # 2. Mock list command polling
        elif "list" in args:
            poll_count += 1
            if poll_count == 1:
                # Still running
                sessions = [
                    {
                        "id": "123",
                        "title": run_id,
                        "status": "running",
                        "tmux_session": "tmux_test_session",
                    }
                ]
            else:
                # Finished
                sessions = [
                    {
                        "id": "123",
                        "title": run_id,
                        "status": "completed",
                        "tmux_session": "tmux_test_session",
                    }
                ]
            return MockCompletedProcess(0, json.dumps(sessions))

        # 3. Mock capture-pane command
        elif "capture-pane" in args:
            assert "tmux_test_session" in args
            return MockCompletedProcess(0, "visual output of mitosis explanation")

        # 4. Mock session stop or remove
        elif "session" in args or "remove" in args:
            return MockCompletedProcess(0, "")

        return MockCompletedProcess(0, "")

    monkeypatch.setattr(adapter_module.subprocess, "run", mock_run)
    monkeypatch.setattr(adapter_module.time, "sleep", lambda x: None)  # speed up polling

    adapter = AgentDeckAdapter()
    result = adapter.run(
        AgentInput(
            run_id=run_id,
            task_id="tsk-123",
            prompt=prompt,
            model="gemini-3.5-flash",
            working_dir="/tmp/work",
        )
    )

    assert result.status == ResultStatus.OK
    assert "mitosis explanation" in result.output_text
    assert result.usage.estimated is True

    # Verify launch got correct -c and model mapped
    launch_args = next(args for args in calls if "launch" in args)
    assert "-c" in launch_args
    c_idx = launch_args.index("-c")
    assert launch_args[c_idx + 1] == "gemini"
    assert "--model" in launch_args
    m_idx = launch_args.index("--model")
    assert launch_args[m_idx + 1] == "gemini-3.5-flash"


def test_cancel_session_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class MockCompletedProcess:
        returncode = 0

    def mock_run(args: list[str], **kwargs: Any) -> Any:
        calls.append(args)
        return MockCompletedProcess()

    monkeypatch.setattr(adapter_module.subprocess, "run", mock_run)

    adapter = AgentDeckAdapter()
    res = adapter.cancel("my-session-ref")
    assert res is True
    assert ["/mock/bin/agent-deck", "session", "stop", "my-session-ref"] in calls


# ---------------------------------------------------------------------------
# Non-result presented as favourable result
# ---------------------------------------------------------------------------
#
# Counterfeit that would fake a fix: always return ERROR (or always return
# whatever the last mocked status string was, without distinguishing
# completed/failed/stopped/missing/timeout). These tests require:
#   - completed → OK
#   - failed / stopped → ERROR with the terminal status in the error text
#   - session never listed → ERROR (not OK with empty output)
#   - poll exhausts without a terminal status → TIMEOUT (not OK)


class _Proc:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _install_agentdeck_poll(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sessions_by_poll: list[list[dict[str, Any]]],
    capture_text: str = "pane text",
) -> list[list[str]]:
    """Drive launch → list polls → optional capture/stop/remove."""
    calls: list[list[str]] = []
    poll_i = 0

    def mock_run(args: list[str], **kwargs: Any) -> Any:
        nonlocal poll_i
        calls.append(list(args))
        if "launch" in args:
            return _Proc(0, "launched")
        if "list" in args:
            if poll_i < len(sessions_by_poll):
                payload = sessions_by_poll[poll_i]
            else:
                payload = sessions_by_poll[-1] if sessions_by_poll else []
            poll_i += 1
            return _Proc(0, json.dumps(payload))
        if "capture-pane" in args:
            return _Proc(0, capture_text)
        return _Proc(0, "")

    monkeypatch.setattr(adapter_module.subprocess, "run", mock_run)
    monkeypatch.setattr(adapter_module.time, "sleep", lambda _x: None)
    return calls


def test_run_failed_session_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal *failed* agent-deck status must not be mapped to ResultStatus.OK.

    Counterfeit: hard-code status=ERROR for every run (including completed).
    Paired with the happy-path test_run_launches_polls_and_captures_stdout
    which still requires completed → OK.
    """
    run_id = "failed-run"
    _install_agentdeck_poll(
        monkeypatch,
        sessions_by_poll=[
            [
                {
                    "id": "1",
                    "title": run_id,
                    "status": "failed",
                    "tmux_session": "tmux_failed",
                }
            ]
        ],
        capture_text="agent crashed",
    )

    result = AgentDeckAdapter().run(
        AgentInput(run_id=run_id, task_id="tsk-fail", prompt="do the thing")
    )

    assert result.status is ResultStatus.ERROR, (
        f"failed session presented as {result.status!r}; unrecorded/failed "
        f"outcomes must not look like success"
    )
    assert result.error and "failed" in result.error.lower()
    # Capture may still succeed; that must not flip the outcome to OK.
    assert "agent crashed" in (result.output_text or "")


def test_run_stopped_session_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal *stopped* status is not success either."""
    run_id = "stopped-run"
    _install_agentdeck_poll(
        monkeypatch,
        sessions_by_poll=[
            [
                {
                    "id": "1",
                    "title": run_id,
                    "status": "stopped",
                    "tmux_session": "tmux_stopped",
                }
            ]
        ],
    )

    result = AgentDeckAdapter().run(
        AgentInput(run_id=run_id, task_id="tsk-stop", prompt="do the thing")
    )
    assert result.status is ResultStatus.ERROR
    assert result.error and "stopped" in result.error.lower()


def test_run_missing_session_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the session never appears in `agent-deck list`, that is not a success.

    Counterfeit: return OK when output_text is empty and error is None — the
    previous bug. An absent session must be ERROR with a reason.
    """
    run_id = "ghost-run"
    _install_agentdeck_poll(monkeypatch, sessions_by_poll=[[]])

    result = AgentDeckAdapter().run(
        AgentInput(run_id=run_id, task_id="tsk-ghost", prompt="do the thing")
    )
    assert result.status is ResultStatus.ERROR
    assert result.error
    assert (
        "never" in result.error.lower()
        or "missing" in result.error.lower()
        or "not found" in result.error.lower()
    )


def test_run_poll_timeout_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exhausting the poll window without a terminal status is TIMEOUT, not OK.

    Counterfeit: map every non-completed status to ERROR, including timeout —
    that still mislabels a hang. Require ResultStatus.TIMEOUT specifically.
    """
    run_id = "slow-run"
    # Always "running" so the loop only ends via timeout.
    _install_agentdeck_poll(
        monkeypatch,
        sessions_by_poll=[
            [
                {
                    "id": "1",
                    "title": run_id,
                    "status": "running",
                    "tmux_session": "tmux_slow",
                }
            ]
        ],
    )
    # Collapse the 30-minute poll window so the loop exits immediately after
    # the first "still running" observation.
    monkeypatch.setattr(
        adapter_module.time, "monotonic", _TimeoutClock(start=1000.0, then=1000.0 + 1801.0)
    )

    result = AgentDeckAdapter().run(
        AgentInput(run_id=run_id, task_id="tsk-slow", prompt="do the thing")
    )
    assert result.status is ResultStatus.TIMEOUT, (
        f"poll timeout presented as {result.status!r}; a hang must not look "
        f"like success or a clean failure"
    )
    assert result.error and "timeout" in result.error.lower()


class _TimeoutClock:
    """monotonic() stand-in: allow one poll iteration, then expire the window."""

    def __init__(self, *, start: float, then: float) -> None:
        self._start = start
        self._then = then
        self._n = 0

    def __call__(self) -> float:
        self._n += 1
        # 1: run start, 2: poll_start, 3: first while-check (enter loop and see
        # the session still running). Later checks jump past poll_timeout.
        if self._n <= 3:
            return self._start
        return self._then
