from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from omniagentos.adapters.qwen import QwenAdapter, _resolve_qwen_cli
from omniagentos.contracts import AgentInput, ResultStatus

from .conftest import FakePopen


def qwen_messages(
    result: str = "hello",
    session_id: str = "qwen-session",
    *,
    with_usage: bool = True,
) -> str:
    terminal: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "session_id": session_id,
        "is_error": False,
        "duration_ms": 500,
        "num_turns": 2,
        "result": result,
    }
    if with_usage:
        terminal["usage"] = {"input_tokens": 30, "output_tokens": 12}
    return json.dumps(
        [
            {
                "type": "system",
                "subtype": "session_start",
                "session_id": session_id,
            },
            terminal,
        ]
    )


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)


def test_qwen_cli_resolution_override_preferred_and_bare_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "override" / "qwen"
    _make_executable(override)
    monkeypatch.setenv("OMNIAGENTOS_QWEN_CLI", str(override))
    assert _resolve_qwen_cli() == str(override)

    monkeypatch.delenv("OMNIAGENTOS_QWEN_CLI")
    fake_home = tmp_path / "home"
    preferred = fake_home / ".local" / "bin" / "qwen"
    _make_executable(preferred)
    assert _resolve_qwen_cli(home=fake_home) == str(preferred)
    assert _resolve_qwen_cli(home=tmp_path / "empty") == "qwen"


def test_qwen_usage_and_read_only_argv(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(qwen_messages(), "", 0, False)]
    result = QwenAdapter().run(input_factory())

    assert result.status is ResultStatus.OK
    assert result.output_text == "hello"
    assert result.session_ref == "qwen-session"
    assert result.usage.input_tokens == 30
    assert result.usage.output_tokens == 12
    assert result.usage.turns == 2
    assert result.usage.cost_usd is None
    assert result.usage.estimated is True
    assert result.usage.source == "mixed"
    command = fake_popen.commands[0]
    assert command[1:6] == ["-p", "say hello", "--output-format", "json", "--approval-mode"]
    assert command[command.index("--approval-mode") + 1] == "plan"
    assert command[command.index("--model") + 1] == "test-model"
    assert fake_popen.prompts[0] is None


def test_qwen_workspace_write_uses_auto_edit_and_threads_budgets_and_dirs(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(qwen_messages(), "", 0, False)]
    QwenAdapter().run(
        input_factory(
            budget={"wall_ms_max": 1_000, "max_turns": 7},
            metadata={
                "sandbox": {"level": "workspace_write"},
                "extra_dirs": ["/drive/Granted"],
            },
        )
    )
    command = fake_popen.commands[0]
    assert command[command.index("--approval-mode") + 1] == "auto-edit"
    assert command[command.index("--max-session-turns") + 1] == "7"
    assert command[-2:] == ["--include-directories", "/drive/Granted"]


def test_qwen_structured_repair_resumes_without_model_override(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [
        (qwen_messages("not json"), "", 0, False),
        (qwen_messages('{"ok": true}'), "", 0, False),
    ]
    result = QwenAdapter().run(input_factory(output_schema={"required": ["ok"]}))

    assert result.status is ResultStatus.OK
    assert result.output_json == {"ok": True}
    repair = fake_popen.commands[1]
    assert repair[repair.index("--resume") + 1] == "qwen-session"
    assert "--model" not in repair


def test_qwen_parse_tolerates_noise_before_json_array() -> None:
    parsed = QwenAdapter()._parse("warning: startup note\n" + qwen_messages())
    assert parsed.text == "hello"
    assert parsed.session_ref == "qwen-session"


def test_qwen_error_result_surfaces_message() -> None:
    output = json.dumps(
        [
            {
                "type": "result",
                "subtype": "error_during_execution",
                "session_id": "qwen-session",
                "is_error": True,
                "error": {"message": "authentication required"},
            }
        ]
    )
    with pytest.raises(ValueError, match="authentication required"):
        QwenAdapter()._parse(output)


def test_qwen_falls_back_to_estimator_without_usage(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(qwen_messages(with_usage=False), "", 0, False)]
    result = QwenAdapter().run(input_factory())
    assert result.status is ResultStatus.OK
    assert result.usage.source == "estimator"
