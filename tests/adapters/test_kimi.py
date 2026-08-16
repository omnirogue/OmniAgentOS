from __future__ import annotations

import json
import signal
from collections.abc import Callable

from omniagentos.adapters.kimi import KimiAdapter
from omniagentos.contracts import AgentInput, ResultStatus, estimate_tokens

from .conftest import FakePopen


def kimi_events(text: str = "hello", session_id: str = "kimi-session") -> str:
    """Mirror kimi's --output-format stream-json writer: JSONL of role-tagged
    messages, ending in the role:"meta" session.resume_hint line that carries
    session_id (see PromptJsonWriter/writeResumeHint in the kimi CLI bundle)."""

    return "\n".join(
        json.dumps(event)
        for event in [
            {"role": "meta", "type": "system.version", "version": "0.24.2"},
            {"role": "assistant", "content": text},
            {
                "role": "meta",
                "type": "session.resume_hint",
                "session_id": session_id,
                "command": f"kimi -r {session_id}",
                "content": f"To resume this session: kimi -r {session_id}",
            },
        ]
    )


def test_kimi_estimates_usage_and_captures_resume_hint_session(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(kimi_events(), "", 0, False)]
    input = input_factory()
    result = KimiAdapter().run(input)

    assert result.status is ResultStatus.OK
    assert result.output_text == "hello"
    assert result.session_ref == "kimi-session"
    assert result.usage.input_tokens == estimate_tokens(input.prompt)
    assert result.usage.output_tokens == estimate_tokens("hello")
    assert result.usage.cost_usd is None
    assert result.usage.estimated is True
    assert result.usage.source == "estimator"
    command = fake_popen.commands[0]
    # Prompt is passed via argv (kimi -p <prompt> is the only option)
    assert command[:5] == ["kimi", "-p", input.prompt, "--output-format", "stream-json"]
    assert command[command.index("-m") + 1] == "test-model"
    assert "-r" not in command


def test_kimi_omits_model_flag_when_unset(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(kimi_events(), "", 0, False)]
    result = KimiAdapter().run(input_factory(model=None))

    assert result.status is ResultStatus.OK
    assert "-m" not in fake_popen.commands[0]


def test_kimi_guards_large_prompt_before_spawn(
    input_factory: Callable[..., AgentInput],
) -> None:
    """Test that prompts exceeding 700KB budget are rejected before spawn.

    The E2BIG guard in _command raises an OSError rather than letting
    posix_spawn fail silently with E2BIG. This test verifies the guard
    fires, preventing spawn attempt.
    """
    large_prompt = "x" * (700 * 1024 + 1000)  # 700KB + 1000 bytes
    input = input_factory(prompt=large_prompt)

    result = KimiAdapter().run(input)

    # Should be ERROR, not TIMEOUT or OK
    assert result.status is ResultStatus.ERROR
    # Error message should mention E2BIG and byte count
    assert "E2BIG" in result.error
    assert "700" in result.error or str(700 * 1024 + 1000) in result.error


def test_kimi_normal_prompt_goes_via_argv(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    """Test that normal-sized prompts (under 700KB) are passed via argv as expected."""
    fake_popen.queued = [(kimi_events(), "", 0, False)]
    normal_prompt = "x" * 50_000  # 50KB, well under budget
    input = input_factory(prompt=normal_prompt)
    result = KimiAdapter().run(input)

    assert result.status is ResultStatus.OK
    command = fake_popen.commands[0]
    # Prompt should be in argv at expected position
    assert command[2] == normal_prompt


def test_kimi_structured_repair_resumes_with_session_id(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [
        (kimi_events("not json"), "", 0, False),
        (kimi_events('{"ok": true}'), "", 0, False),
    ]
    result = KimiAdapter().run(input_factory(output_schema={"required": ["ok"]}))

    assert result.status is ResultStatus.OK
    assert result.output_json == {"ok": True}
    assert fake_popen.commands[1][fake_popen.commands[1].index("-r") + 1] == "kimi-session"
    # Error repair appends to prompt
    assert "response is not valid JSON" in fake_popen.prompts[1]


def test_kimi_adds_add_dir_for_extra_dirs(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(kimi_events(), "", 0, False)]
    # Carry the same cli_unattended_elevated flag the conftest default supplies:
    # kimi is force-auto (honors_read_only_sandbox=False) and the guardrail refuses
    # an unelevated force-auto CLI before argv is built (AC-policy fix4 BLOCKER 2).
    # This test asserts the Drive --add-dir argv construction, so it runs kimi in the
    # allowed/elevated context; the refusal itself is covered by the guardrail tests.
    input = input_factory(
        metadata={
            "extra_dirs": ["/vault/CopywritingBrainVault", "/drive/OmniAgent"],
            "cli_unattended_elevated": True,
        }
    )
    KimiAdapter().run(input)
    cmd = fake_popen.commands[0]
    assert cmd[-4:] == [
        "--add-dir",
        "/vault/CopywritingBrainVault",
        "--add-dir",
        "/drive/OmniAgent",
    ]


def test_kimi_omits_add_dir_when_no_extra_dirs(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(kimi_events(), "", 0, False)]
    KimiAdapter().run(input_factory())
    assert "--add-dir" not in fake_popen.commands[0]


def test_kimi_nonzero_exit_is_error(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [("", "kimi unavailable", 1, False)]
    result = KimiAdapter().run(input_factory())

    assert result.status is ResultStatus.ERROR
    assert result.error == "kimi unavailable"


def test_kimi_timeout_terminates_its_process_group(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [("", "", -15, True)]
    result = KimiAdapter().run(input_factory(budget={"wall_ms_max": 1}))

    assert result.status is ResultStatus.TIMEOUT
    assert result.usage.wall_ms >= 1
    assert fake_popen.signals == [signal.SIGTERM]
