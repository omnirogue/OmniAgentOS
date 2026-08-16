from __future__ import annotations

import json
import signal
from collections.abc import Callable

import pytest

from omniagentos.adapters.grok import GrokAdapter
from omniagentos.contracts import AgentInput, ResultStatus, estimate_tokens

from .conftest import FakePopen


def grok_envelope(text: str = "hello") -> str:
    return json.dumps({"text": text, "sessionId": "grok-session"})


def test_grok_estimates_usage_and_honors_sandbox(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [(grok_envelope(), "", 0, False)]
    input = input_factory(metadata={"sandbox": {"level": "workspace_write"}})
    result = GrokAdapter().run(input)

    assert result.status is ResultStatus.OK
    assert result.session_ref == "grok-session"
    assert result.usage.input_tokens == estimate_tokens(input.prompt)
    assert result.usage.output_tokens == estimate_tokens("hello")
    assert result.usage.cost_usd is None
    assert result.usage.estimated is True
    assert result.usage.source == "estimator"
    command = fake_popen.commands[0]
    # grok's REAL profile vocabulary: workspace, not codex's workspace-write
    # (which made every live launch refuse to start).
    assert command[command.index("--sandbox") + 1] == "workspace"


def test_grok_has_no_add_dir_equivalent_for_extra_dirs(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    # Documented limitation (see adapters/grok.py): this CLI version has no
    # multi-directory allowlist flag, so extra_dirs is present on metadata but
    # intentionally never reaches argv -- a scoped grok run stays confined to
    # --cwd until the CLI grows an equivalent.
    fake_popen.queued = [(grok_envelope(), "", 0, False)]
    input = input_factory(
        metadata={
            "sandbox": {"level": "workspace_write"},
            "extra_dirs": ["/vault/CopywritingBrainVault"],
        }
    )
    GrokAdapter().run(input)
    cmd = fake_popen.commands[0]
    assert "--add-dir" not in cmd
    assert "/vault/CopywritingBrainVault" not in cmd


def test_grok_read_only_sandbox_profile_when_wrap_unavailable(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    # Wrap NOT engaged (conftest default): grok's own sandbox is the sole
    # confinement and the original pair must be preserved.
    fake_popen.queued = [(grok_envelope(), "", 0, False)]
    GrokAdapter().run(input_factory())
    command = fake_popen.commands[0]
    assert command[command.index("--sandbox") + 1] == "read-only"


def test_grok_sandbox_pair_removed_under_outer_wrap(
    monkeypatch: pytest.MonkeyPatch,
    fake_popen: type[FakePopen],
    input_factory: Callable[..., AgentInput],
) -> None:
    """Under the outer Seatbelt wrap grok's --sandbox pair is REMOVED entirely
    (D2): grok rejects codex vocabulary and nested Seatbelt cannot apply, so
    the CLI runs at its documented default (off) with the outer profile as the
    enforcement layer."""
    monkeypatch.setattr(
        "omniagentos.runner.sandbox.wrap_available", lambda argv, workspace_dir: True
    )
    for level in ("workspace_write", "read_only"):
        fake_popen.queued = [(grok_envelope(), "", 0, False)]
        GrokAdapter().run(
            input_factory(metadata={"sandbox": {"level": level}, "cli_unattended_elevated": True})
        )
        command = fake_popen.commands[-1]
        assert "--sandbox" not in command
        assert "workspace" not in command
        assert "read-only" not in command
        assert "danger-full-access" not in command


def test_grok_parse_tolerates_noise_before_trailing_envelope() -> None:
    """provider_exec merges stderr into stdout, so a clean rc=0 grok stream may
    carry warning noise before the JSON envelope — the parse must survive it."""
    noise_stream = (
        "warning: some startup notice\n"
        "Loaded profile defaults {not json here\n"
        + json.dumps({"text": "task done", "sessionId": "grok-abc"}, indent=1)
        + "\n"
    )
    parsed = GrokAdapter()._parse(noise_stream)
    assert parsed.text == "task done"
    assert parsed.session_ref == "grok-abc"


def test_grok_parse_ignores_prose_json_without_envelope_keys() -> None:
    stream = 'preamble {"unrelated": 1} trailing words\n'
    with pytest.raises(ValueError, match="no JSON envelope"):
        GrokAdapter()._parse(stream)


def test_grok_parse_no_json_at_all_still_raises() -> None:
    with pytest.raises(ValueError, match="no JSON envelope"):
        GrokAdapter()._parse("plain text, nothing else\n")


def test_grok_parse_success_envelope_beats_later_error_blob() -> None:
    """F4 pin: a success envelope followed by a stray error blob on the merged
    stream must still parse as the success."""
    stream = (
        "warning noise\n"
        + json.dumps({"text": "task done", "sessionId": "grok-abc"})
        + "\n"
        + json.dumps({"error": "late transport hiccup"})
        + "\n"
    )
    parsed = GrokAdapter()._parse(stream)
    assert parsed.text == "task done"
    assert parsed.session_ref == "grok-abc"


def test_grok_parse_error_only_surfaces_real_message() -> None:
    """F4 pin (other direction): no success-shaped object at all -> the last
    'error'-keyed object is the envelope and its real message raises."""
    stream = "noise\n" + json.dumps({"error": {"message": "invalid bearer token"}}) + "\n"
    with pytest.raises(ValueError, match="invalid bearer token"):
        GrokAdapter()._parse(stream)


def test_grok_nonzero_exit_is_error(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [("", "grok unavailable", 1, False)]
    result = GrokAdapter().run(input_factory())

    assert result.status is ResultStatus.ERROR
    assert result.error == "grok unavailable"


def test_grok_timeout_terminates_its_process_group(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    fake_popen.queued = [("", "", -15, True)]
    result = GrokAdapter().run(input_factory(budget={"wall_ms_max": 1}))

    assert result.status is ResultStatus.TIMEOUT
    assert result.usage.wall_ms >= 1
    assert fake_popen.signals == [signal.SIGTERM]
