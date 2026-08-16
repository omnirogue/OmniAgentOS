"""Tests for FusionHarness (omniagentos/harnesses/fusion.py).

Tests use mocked subprocesses to avoid spawning real fusion swarms (which would
be expensive in tokens). The mock Popen return canned JSON responses that match
the claude CLI envelope format, so the harness's parsing logic is exercised
without any real network calls or multi-agent sessions.

Key behavior tested:
- resolve_adapter(HarnessType.FUSION) returns FusionHarness
- run() builds correct command argv: claude -p "/<entrypoint> <prompt>" ...
- subprocess is mocked to avoid real swarm execution
- health() reflects CLI availability
- cancel() terminates subprocess
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from omniagentos.adapters import registry
from omniagentos.contracts import (
    AgentAdapter,
    AgentInput,
    HarnessType,
    HealthStatus,
    ResultStatus,
)
from omniagentos.harnesses.fusion import (
    FusionHarness,
    _resolve_claude_cli,
    _resolve_fusion_entrypoint,
)

# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------


def test_isinstance_agent_adapter() -> None:
    assert isinstance(FusionHarness(), AgentAdapter)
    assert FusionHarness.name == "fusion"


def test_resolves_via_registry_import_path() -> None:
    # Verify the registry can import and instantiate it correctly
    adapter = registry.resolve_adapter(HarnessType.FUSION)
    assert isinstance(adapter, FusionHarness)
    assert adapter.name == "fusion"


def test_registry_returns_cached_singleton() -> None:
    # Verify caching behavior
    adapter1 = registry.resolve_adapter(HarnessType.FUSION)
    adapter2 = registry.resolve_adapter(HarnessType.FUSION)
    assert adapter1 is adapter2


# ---------------------------------------------------------------------------
# CLI resolution
# ---------------------------------------------------------------------------


def test_resolve_claude_cli_default() -> None:
    with patch.dict(os.environ, {}, clear=True):
        # Should resolve to ~/.local/bin/claude or "claude"
        result = _resolve_claude_cli()
        assert isinstance(result, str)
        assert "claude" in result


def test_resolve_claude_cli_respects_env_override() -> None:
    with patch.dict(os.environ, {"OMNIAGENTOS_CLAUDE_CLI": "/custom/claude"}):
        result = _resolve_claude_cli()
        assert result == "/custom/claude"


def test_resolve_claude_cli_uses_preferred_path_when_executable() -> None:
    with patch.dict(os.environ, {}, clear=True):
        fake_home = Path("/fake/home")
        with patch("pathlib.Path.is_file", return_value=True):
            with patch("os.access", return_value=True):
                result = _resolve_claude_cli(home=fake_home)
                assert "/fake/home/.local/bin/claude" in result


# ---------------------------------------------------------------------------
# Entrypoint resolution
# ---------------------------------------------------------------------------


def test_resolve_fusion_entrypoint_default() -> None:
    with patch.dict(os.environ, {}, clear=True):
        result = _resolve_fusion_entrypoint()
        assert result == "/fusion"


def test_resolve_fusion_entrypoint_respects_env_override() -> None:
    with patch.dict(os.environ, {"OMNIAGENTOS_FUSION_ENTRYPOINT": "/ultrabuild"}):
        result = _resolve_fusion_entrypoint()
        assert result == "/ultrabuild"


def test_resolve_fusion_entrypoint_superfast() -> None:
    with patch.dict(os.environ, {"OMNIAGENTOS_FUSION_ENTRYPOINT": "/superfast"}):
        result = _resolve_fusion_entrypoint()
        assert result == "/superfast"


# ---------------------------------------------------------------------------
# Command building (mocked subprocess)
# ---------------------------------------------------------------------------


def test_run_builds_correct_command_argv() -> None:
    """Verify the command shape: claude -p "/<entrypoint> <prompt>" --output-format json"""
    harness = FusionHarness()
    harness.cli = "/mock/claude"

    # Mock Popen to capture the command
    captured_command = []

    class MockPopen:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            captured_command.append(command)
            self.pid = 12345
            self.returncode = 0
            self.stdout = ""
            self.stderr = ""

        def communicate(
            self, input: str | None = None, timeout: int | None = None
        ) -> tuple[str, str]:
            # Return a valid JSON response envelope
            response = {
                "result": "fusion swarm completed successfully",
                "is_error": False,
                "session_id": "sess_123",
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "total_cost_usd": 0.01,
                "num_turns": 2,
            }
            return json.dumps(response), ""

        def poll(self) -> int | None:
            return self.returncode

        def kill(self) -> None:
            pass

    with patch("subprocess.Popen", MockPopen):
        with patch.dict(os.environ, {"OMNIAGENTOS_FUSION_ENTRYPOINT": "/fusion"}):
            harness.run(
                AgentInput(
                    run_id="run_test",
                    task_id="tsk_test",
                    prompt="Implement a feature",
                    working_dir="/tmp/work",
                )
            )

    assert len(captured_command) == 1
    cmd = captured_command[0]
    assert cmd[0] == "/mock/claude"
    assert "-p" in cmd
    assert "--output-format" in cmd
    assert "json" in cmd
    # The prompt should be prefixed with the entrypoint
    prompt_idx = cmd.index("-p") + 1
    assert "/fusion " in cmd[prompt_idx]
    assert "Implement a feature" in cmd[prompt_idx]


def test_run_uses_custom_entrypoint() -> None:
    """Verify custom entrypoint is used when set."""
    harness = FusionHarness()
    harness.cli = "/mock/claude"

    captured_command = []

    class MockPopen:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            captured_command.append(command)
            self.pid = 12345
            self.returncode = 0

        def communicate(
            self, input: str | None = None, timeout: int | None = None
        ) -> tuple[str, str]:
            response = {"result": "ok", "is_error": False}
            return json.dumps(response), ""

        def poll(self) -> int | None:
            return 0

        def kill(self) -> None:
            pass

    with patch("subprocess.Popen", MockPopen):
        with patch.dict(os.environ, {"OMNIAGENTOS_FUSION_ENTRYPOINT": "/ultrabuild"}):
            harness.run(
                AgentInput(run_id="run_1", task_id="tsk_1", prompt="Test task", working_dir="/tmp")
            )

    cmd = captured_command[0]
    prompt_idx = cmd.index("-p") + 1
    assert "/ultrabuild " in cmd[prompt_idx]


def test_run_success_parses_json_response() -> None:
    """Verify successful run parses JSON and returns correct AgentResult."""
    harness = FusionHarness()
    harness.cli = "/mock/claude"

    response_obj = {
        "result": "Task completed successfully",
        "is_error": False,
        "session_id": "sess_456",
        "usage": {"input_tokens": 200, "output_tokens": 100},
        "total_cost_usd": 0.05,
        "num_turns": 3,
    }

    class MockPopen:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            self.pid = 12345
            self.returncode = 0

        def communicate(
            self, input: str | None = None, timeout: int | None = None
        ) -> tuple[str, str]:
            return json.dumps(response_obj), ""

        def poll(self) -> int | None:
            return 0

        def kill(self) -> None:
            pass

    with patch("subprocess.Popen", MockPopen):
        result = harness.run(
            AgentInput(run_id="run_1", task_id="tsk_1", prompt="Test", working_dir="/tmp")
        )

    assert result.status == ResultStatus.OK
    assert result.output_text == "Task completed successfully"
    assert result.session_ref == "sess_456"
    assert result.usage.input_tokens == 200
    assert result.usage.output_tokens == 100
    assert result.usage.cost_usd == 0.05
    assert result.usage.turns == 3
    assert result.usage.estimated is False
    assert result.usage.source == "cli-report"


def test_run_fallback_to_estimator_when_no_usage_info() -> None:
    """Verify fallback to estimator when usage info is missing."""
    harness = FusionHarness()
    harness.cli = "/mock/claude"

    response_obj = {"result": "Task done", "is_error": False}

    class MockPopen:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            self.pid = 12345
            self.returncode = 0

        def communicate(
            self, input: str | None = None, timeout: int | None = None
        ) -> tuple[str, str]:
            return json.dumps(response_obj), ""

        def poll(self) -> int | None:
            return 0

        def kill(self) -> None:
            pass

    with patch("subprocess.Popen", MockPopen):
        result = harness.run(
            AgentInput(run_id="run_1", task_id="tsk_1", prompt="Test prompt", working_dir="/tmp")
        )

    assert result.status == ResultStatus.OK
    assert result.usage.estimated is True
    assert result.usage.source == "estimator"
    assert result.usage.input_tokens > 0  # estimated from prompt


def test_run_process_error_returns_error_result() -> None:
    """Verify error in subprocess is mapped to AgentResult error."""
    harness = FusionHarness()
    harness.cli = "/mock/claude"

    class MockPopen:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            self.pid = 12345
            self.returncode = 1

        def communicate(
            self, input: str | None = None, timeout: int | None = None
        ) -> tuple[str, str]:
            return "", "Process error: something went wrong"

        def poll(self) -> int | None:
            return 1

        def kill(self) -> None:
            pass

    with patch("subprocess.Popen", MockPopen):
        result = harness.run(
            AgentInput(run_id="run_1", task_id="tsk_1", prompt="Test", working_dir="/tmp")
        )

    assert result.status == ResultStatus.ERROR
    assert result.error is not None
    assert "went wrong" in result.error


def test_run_invalid_json_returns_error() -> None:
    """Verify malformed JSON in response is handled."""
    harness = FusionHarness()
    harness.cli = "/mock/claude"

    class MockPopen:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            self.pid = 12345
            self.returncode = 0

        def communicate(
            self, input: str | None = None, timeout: int | None = None
        ) -> tuple[str, str]:
            return "not valid json", ""

        def poll(self) -> int | None:
            return 0

        def kill(self) -> None:
            pass

    with patch("subprocess.Popen", MockPopen):
        result = harness.run(
            AgentInput(run_id="run_1", task_id="tsk_1", prompt="Test", working_dir="/tmp")
        )

    assert result.status == ResultStatus.ERROR
    assert "JSON" in result.error


def test_run_error_envelope_is_detected() -> None:
    """Verify error envelope (is_error=true) is recognized."""
    harness = FusionHarness()
    harness.cli = "/mock/claude"

    response_obj = {
        "result": "Something failed",
        "is_error": True,
    }

    class MockPopen:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            self.pid = 12345
            self.returncode = 0

        def communicate(
            self, input: str | None = None, timeout: int | None = None
        ) -> tuple[str, str]:
            return json.dumps(response_obj), ""

        def poll(self) -> int | None:
            return 0

        def kill(self) -> None:
            pass

    with patch("subprocess.Popen", MockPopen):
        result = harness.run(
            AgentInput(run_id="run_1", task_id="tsk_1", prompt="Test", working_dir="/tmp")
        )

    assert result.status == ResultStatus.ERROR
    assert "Something failed" in result.error


def test_run_timeout_returns_timeout_result() -> None:
    """Verify subprocess timeout is mapped to TIMEOUT status."""
    harness = FusionHarness()
    harness.cli = "/mock/claude"

    class MockPopen:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            self.pid = 12345

        def communicate(
            self, input: str | None = None, timeout: int | None = None
        ) -> tuple[str, str]:
            raise subprocess.TimeoutExpired("cmd", 300)

        def poll(self) -> int | None:
            return None

        def kill(self) -> None:
            pass

    with patch("subprocess.Popen", MockPopen):
        result = harness.run(
            AgentInput(run_id="run_1", task_id="tsk_1", prompt="Test", working_dir="/tmp")
        )

    assert result.status == ResultStatus.TIMEOUT
    assert "timed out" in result.error


def test_run_exception_is_caught_and_mapped() -> None:
    """Verify subprocess exceptions are caught and mapped to AgentResult."""
    harness = FusionHarness()
    harness.cli = "/mock/claude"

    class MockPopen:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            raise OSError("Cannot start process")

    with patch("subprocess.Popen", MockPopen):
        result = harness.run(
            AgentInput(run_id="run_1", task_id="tsk_1", prompt="Test", working_dir="/tmp")
        )

    assert result.status == ResultStatus.ERROR
    assert "Cannot start process" in result.error


def test_run_includes_model_in_command_when_specified() -> None:
    """Verify --model is added to command when input.model is set."""
    harness = FusionHarness()
    harness.cli = "/mock/claude"

    captured_command = []

    class MockPopen:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            captured_command.append(command)
            self.pid = 12345
            self.returncode = 0

        def communicate(
            self, input: str | None = None, timeout: int | None = None
        ) -> tuple[str, str]:
            return json.dumps({"result": "ok", "is_error": False}), ""

        def poll(self) -> int | None:
            return 0

        def kill(self) -> None:
            pass

    with patch("subprocess.Popen", MockPopen):
        harness.run(
            AgentInput(
                run_id="run_1",
                task_id="tsk_1",
                prompt="Test",
                working_dir="/tmp",
                model="opus",
            )
        )

    cmd = captured_command[0]
    assert "--model" in cmd
    assert "opus" in cmd


def test_run_respects_working_dir() -> None:
    """Verify working_dir is passed to subprocess cwd."""
    harness = FusionHarness()
    harness.cli = "/mock/claude"

    captured_cwd = []

    class MockPopen:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            captured_cwd.append(kwargs.get("cwd"))
            self.pid = 12345
            self.returncode = 0

        def communicate(
            self, input: str | None = None, timeout: int | None = None
        ) -> tuple[str, str]:
            return json.dumps({"result": "ok", "is_error": False}), ""

        def poll(self) -> int | None:
            return 0

        def kill(self) -> None:
            pass

    with patch("subprocess.Popen", MockPopen):
        harness.run(
            AgentInput(
                run_id="run_1",
                task_id="tsk_1",
                prompt="Test",
                working_dir="/custom/dir",
            )
        )

    assert captured_cwd[0] == "/custom/dir"


# ---------------------------------------------------------------------------
# cancel() behavior
# ---------------------------------------------------------------------------


def test_cancel_kills_active_process() -> None:
    """Verify cancel() terminates the subprocess."""
    harness = FusionHarness()

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # Still running
    harness._active["sess_123"] = mock_proc

    result = harness.cancel("sess_123")

    assert result is True
    mock_proc.kill.assert_called_once()


def test_cancel_returns_false_for_nonexistent_session() -> None:
    """Verify cancel() returns False when session is not found."""
    harness = FusionHarness()
    result = harness.cancel("nonexistent")
    assert result is False


def test_cancel_returns_false_for_already_completed() -> None:
    """Verify cancel() returns False when process already finished."""
    harness = FusionHarness()

    mock_proc = MagicMock()
    mock_proc.poll.return_value = 0  # Already exited
    harness._active["sess_123"] = mock_proc

    result = harness.cancel("sess_123")

    assert result is False
    mock_proc.kill.assert_not_called()


def test_cancel_handles_kill_exceptions() -> None:
    """Verify cancel() handles OSError from kill()."""
    harness = FusionHarness()

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # Still running
    mock_proc.kill.side_effect = OSError("No such process")
    harness._active["sess_123"] = mock_proc

    result = harness.cancel("sess_123")

    assert result is False


# ---------------------------------------------------------------------------
# health() behavior
# ---------------------------------------------------------------------------


def test_health_returns_health_status_shape() -> None:
    """Verify health() returns proper HealthStatus."""
    harness = FusionHarness()
    harness.cli = "claude"

    # Mock subprocess.run to succeed
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "claude version 1.0"
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        result = harness.health()

    assert isinstance(result, HealthStatus)
    assert isinstance(result.healthy, bool)
    assert isinstance(result.detail, str)
    assert isinstance(result.capabilities, dict)
    assert "live_runs" in result.capabilities


def test_health_reports_healthy_when_cli_available() -> None:
    """Verify health() reports healthy when CLI responds."""
    harness = FusionHarness()

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "claude --version output"
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        result = harness.health()

    assert result.healthy is True
    assert result.capabilities["live_runs"] is True


def test_health_reports_unhealthy_when_cli_unavailable() -> None:
    """Verify health() reports unhealthy when CLI fails."""
    harness = FusionHarness()

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    mock_result.stderr = "command not found"

    with patch("subprocess.run", return_value=mock_result):
        result = harness.health()

    assert result.healthy is False
    assert result.capabilities["live_runs"] is False


def test_health_reports_unhealthy_on_exception() -> None:
    """Verify health() handles subprocess exceptions."""
    harness = FusionHarness()

    with patch("subprocess.run", side_effect=OSError("Cannot execute")):
        result = harness.health()

    assert result.healthy is False
    assert result.capabilities["live_runs"] is False
    assert "unavailable" in result.detail


def test_health_caches_result() -> None:
    """Verify health() caches its result."""
    harness = FusionHarness()

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "version"
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result) as mock_run:
        result1 = harness.health()
        result2 = harness.health()

    # subprocess.run should only be called once due to caching
    assert mock_run.call_count == 1
    assert result1 is result2
