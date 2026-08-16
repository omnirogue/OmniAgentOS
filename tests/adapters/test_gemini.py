from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from omniagentos.adapters.gemini import GeminiAdapter
from omniagentos.contracts import AgentInput, ResultStatus

from .conftest import FakePopen


def gemini_envelope(
    response: str = "hello", session_id: str = "gemini-session", *, with_stats: bool = True
) -> str:
    """Mirror `gemini -o json`'s single pretty-printed envelope (see
    JsonFormatter.format / StreamJsonFormatter.convertToStreamStats in
    @google/gemini-cli-core)."""

    payload: dict[str, object] = {"session_id": session_id, "response": response}
    if with_stats:
        payload["stats"] = {
            "total_tokens": 42,
            "input_tokens": 30,
            "output_tokens": 12,
            "cached": 0,
            "input": 30,
            "duration_ms": 500,
            "tool_calls": 0,
        }
    return json.dumps(payload)


def test_gemini_reports_token_usage_and_auto_edit_approval_mode(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(gemini_envelope(), "", 0, False)]
    input = input_factory()
    result = GeminiAdapter().run(input)

    assert result.status is ResultStatus.OK
    assert result.output_text == "hello"
    assert result.session_ref == "gemini-session"
    assert result.usage.input_tokens == 30
    assert result.usage.output_tokens == 12
    assert result.usage.cost_usd is None
    assert result.usage.estimated is True
    assert result.usage.source == "mixed"
    command = fake_popen.commands[0]
    assert command[:4] == ["gemini", "-p", input.prompt, "-o"]
    # auto_edit: headless sessions have no approver — `default` silently kills
    # every edit tool (workers answer in chat, mechanical verify denies).
    assert command[command.index("--approval-mode") + 1] == "auto_edit"
    assert command[command.index("-m") + 1] == "test-model"
    assert "--resume" not in command


def test_gemini_uses_working_default_model(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(gemini_envelope(), "", 0, False)]
    GeminiAdapter().run(input_factory(model=""))

    command = fake_popen.commands[0]
    assert command[command.index("-m") + 1] == "gemini-3.1-pro-preview"


def test_gemini_falls_back_to_estimator_without_stats(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(gemini_envelope(with_stats=False), "", 0, False)]
    result = GeminiAdapter().run(input_factory())

    assert result.status is ResultStatus.OK
    assert result.usage.estimated is True
    assert result.usage.source == "estimator"


def test_gemini_error_envelope_raises_before_success(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    payload = json.dumps(
        {"session_id": "gemini-session", "error": {"type": "FatalInputError", "message": "no auth"}}
    )
    fake_popen.queued = [(payload, "", 0, False)]
    result = GeminiAdapter().run(input_factory())

    assert result.status is ResultStatus.ERROR
    assert result.error == "no auth"


def test_gemini_structured_repair_resumes_by_session_id(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [
        (gemini_envelope("not json"), "", 0, False),
        (gemini_envelope('{"ok": true}'), "", 0, False),
    ]
    result = GeminiAdapter().run(input_factory(output_schema={"required": ["ok"]}))

    assert result.status is ResultStatus.OK
    assert result.output_json == {"ok": True}
    assert fake_popen.commands[1][fake_popen.commands[1].index("--resume") + 1] == "gemini-session"
    assert "-m" not in fake_popen.commands[1]


def test_gemini_adds_include_directories_for_extra_dirs(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(gemini_envelope(), "", 0, False)]
    input = input_factory(
        metadata={"extra_dirs": ["/vault/CopywritingBrainVault", "/drive/OmniAgent"]}
    )
    GeminiAdapter().run(input)
    cmd = fake_popen.commands[0]
    assert cmd[-4:] == [
        "--include-directories",
        "/vault/CopywritingBrainVault",
        "--include-directories",
        "/drive/OmniAgent",
    ]


def test_gemini_omits_include_directories_when_no_extra_dirs(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(gemini_envelope(), "", 0, False)]
    GeminiAdapter().run(input_factory())
    assert "--include-directories" not in fake_popen.commands[0]


# Live merged-stream shape (stderr folded into stdout by provider_exec's
# stderr=subprocess.STDOUT): noise lines, node stack frames, a sandboxed EPERM
# error-report write, then the pretty-printed envelope at exit — modeled on
# sessions row ses_fcddc6b55e2842cb8d8b.
def _noisy_stream(envelope: str) -> str:
    return (
        "Loaded cached credentials.\n"
        "Error when talking to Gemini API something {retryable}\n"
        "    at file:///opt/gemini/dist/geminiChat.js:445:32\n"
        "EPERM: operation not permitted, open "
        "'/var/folders/zz/T/gemini-client-error-2026.json'\n" + envelope + "\n"
    )


def test_gemini_parse_tolerates_noise_before_trailing_envelope() -> None:
    stream = _noisy_stream(json.dumps({"session_id": "b78ebd83", "response": "all good"}, indent=2))
    parsed = GeminiAdapter()._parse(stream)
    assert parsed.text == "all good"
    assert parsed.session_ref == "b78ebd83"


def test_gemini_run_completes_despite_stderr_noise(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(_noisy_stream(gemini_envelope()), "", 0, False)]
    result = GeminiAdapter().run(input_factory())
    assert result.status is ResultStatus.OK
    assert result.output_text == "hello"
    assert result.session_ref == "gemini-session"


def test_gemini_parse_noisy_error_envelope_raises_real_message() -> None:
    stream = _noisy_stream(
        json.dumps(
            {
                "session_id": "b78ebd83",
                "error": {"type": "Error", "message": "[object Object]", "code": 1},
            },
            indent=1,
        )
    )
    with pytest.raises(ValueError, match=r"\[object Object\]"):
        GeminiAdapter()._parse(stream)


def test_gemini_parse_ignores_prose_json_without_envelope_keys() -> None:
    with pytest.raises(ValueError, match="no JSON envelope"):
        GeminiAdapter()._parse('some text {"random": true} more text\n')


def test_gemini_parse_last_envelope_wins_over_prose_object() -> None:
    """A JSON object the model printed in prose earlier must lose to the real
    trailing envelope."""
    stream = (
        'model prose: {"response": "fake early", "session_id": "nope"}\n'
        "more noise\n" + json.dumps({"session_id": "real", "response": "real answer"})
    )
    parsed = GeminiAdapter()._parse(stream)
    assert parsed.text == "real answer"
    assert parsed.session_ref == "real"


def test_gemini_parse_success_envelope_beats_later_error_blob() -> None:
    """F4 pin: a clean success envelope followed by a stray error blob on the
    merged stream (e.g. a failed error-report write dumped as JSON) must still
    parse as the success — the error object only surfaces when NO
    success-shaped envelope exists."""
    stream = (
        _noisy_stream(json.dumps({"session_id": "b78ebd83", "response": "all good"}))
        + json.dumps({"error": {"type": "Error", "message": "late noise"}})
        + "\n"
    )
    parsed = GeminiAdapter()._parse(stream)
    assert parsed.text == "all good"
    assert parsed.session_ref == "b78ebd83"


def test_gemini_parse_error_only_still_surfaces_error() -> None:
    """F4 pin (other direction): with no success-shaped object at all, the last
    'error'-keyed object is the envelope and its real message raises."""
    stream = "noise line\n" + json.dumps({"error": {"type": "Error", "message": "quota gone"}})
    with pytest.raises(ValueError, match="quota gone"):
        GeminiAdapter()._parse(stream)


def test_gemini_nonzero_exit_is_error(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [("", "gemini unavailable", 1, False)]
    result = GeminiAdapter().run(input_factory())

    assert result.status is ResultStatus.ERROR
    assert result.error == "gemini unavailable"
