"""T4.5 — reasoning effort must not be silently dropped.

Before this, effort travelled to the adapters down two metadata keys that never
met: ``metadata["effort"]`` (written by intake/lab, read only by the Claude
adapter) and ``metadata["reasoning_effort"]`` (written by swarm's provider-exec,
read only by codex/grok). Swarm's router-decided effort therefore reached zero
Claude workers, and intake/lab's effort reached zero codex/grok workers.

These tests pin the converged read side: one precedence order, one canonical
vocabulary, and an explicit per-CLI capability mapping.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from omniagentos.adapters.claude import ClaudeAdapter
from omniagentos.adapters.common import (
    cli_reasoning_effort,
    requested_reasoning_effort,
    resolve_reasoning_effort,
)
from omniagentos.contracts import AgentInput, ExecutionEnvelope, ReasoningEffort

from .conftest import FakePopen


def claude_envelope() -> str:
    return (
        '{"type": "result", "result": "hi", "session_id": "s", "num_turns": 1, '
        '"total_cost_usd": 0.01, "usage": {"input_tokens": 5, "output_tokens": 2}}'
    )


def claude_argv_effort(fake_popen: type[FakePopen], input: AgentInput) -> str | None:
    """Run the Claude adapter offline and return the emitted ``--effort`` value."""
    fake_popen.queued = [(claude_envelope(), "", 0, False)]
    ClaudeAdapter().run(input)
    cmd = fake_popen.commands[-1]
    if "--effort" not in cmd:
        return None
    return cmd[cmd.index("--effort") + 1]


# ---------------------------------------------------------------------------
# Precedence: envelope > metadata["reasoning_effort"] > metadata["effort"]
# ---------------------------------------------------------------------------


def test_envelope_wins_over_both_legacy_keys(input_factory: Callable[..., AgentInput]) -> None:
    input = input_factory(
        envelope=ExecutionEnvelope(effort=ReasoningEffort.XHIGH),
        metadata={"reasoning_effort": "medium", "effort": "low"},
    )
    assert resolve_reasoning_effort(input) == "xhigh"


def test_reasoning_effort_wins_over_effort_without_envelope(
    input_factory: Callable[..., AgentInput],
) -> None:
    input = input_factory(metadata={"reasoning_effort": "high", "effort": "low"})
    assert resolve_reasoning_effort(input) == "high"


def test_effort_alone_resolves(input_factory: Callable[..., AgentInput]) -> None:
    assert resolve_reasoning_effort(input_factory(metadata={"effort": "medium"})) == "medium"


def test_nothing_set_is_a_clean_no_op(input_factory: Callable[..., AgentInput]) -> None:
    input = input_factory()
    # Default AgentInput carries an all-None envelope and no effort metadata.
    assert input.envelope.effort is None
    assert resolve_reasoning_effort(input) is None
    assert cli_reasoning_effort(input, "claude") is None
    assert requested_reasoning_effort(input) is None


@pytest.mark.parametrize(
    "metadata",
    [
        {"reasoning_effort": "not-an-effort", "effort": "high"},
        {"reasoning_effort": "", "effort": "high"},
        {"reasoning_effort": 7, "effort": "high"},
    ],
)
def test_unrecognized_higher_precedence_value_falls_through(
    input_factory: Callable[..., AgentInput], metadata: dict[str, object]
) -> None:
    """A garbage value at a higher-precedence key must not mask a good one.

    Metadata is untrusted config-shaped input that lands in argv, so the bad
    value is dropped -- but dropping it must not also drop the valid key below."""
    assert resolve_reasoning_effort(input_factory(metadata=metadata)) == "high"


def test_values_are_normalized_case_and_whitespace_insensitively(
    input_factory: Callable[..., AgentInput],
) -> None:
    assert resolve_reasoning_effort(input_factory(metadata={"effort": "  XHigh "})) == "xhigh"


def test_garbage_everywhere_degrades_to_none(input_factory: Callable[..., AgentInput]) -> None:
    input = input_factory(metadata={"reasoning_effort": "ultra", "effort": "turbo"})
    assert resolve_reasoning_effort(input) is None
    assert cli_reasoning_effort(input, "claude") is None


# ---------------------------------------------------------------------------
# The regression this task exists for: swarm-shaped metadata reaching Claude.
# ---------------------------------------------------------------------------


def test_swarm_router_effort_now_reaches_the_claude_adapter(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    """THE BUG. ``swarm/provider_exec.py`` writes exactly this metadata shape for
    a Claude worker (configs/swarm.yaml pins claude-opus-5 to xhigh at the
    complex tier). Before T4.5 the Claude adapter read only metadata["effort"],
    so this argv had no ``--effort`` at all and the router's decision was lost."""
    input = input_factory(
        model="claude-opus-5",
        metadata={
            "sandbox": {"level": "workspace_write"},
            "cli_unattended_elevated": True,
            "reasoning_effort": "xhigh",
        },
    )
    assert claude_argv_effort(fake_popen, input) == "xhigh"


def test_intake_effort_now_reaches_codex_and_grok(input_factory: Callable[..., AgentInput]) -> None:
    """The mirror half: intake/lab write metadata["effort"], which codex and grok
    could not see because they read only metadata["reasoning_effort"]."""
    assert requested_reasoning_effort(input_factory(metadata={"effort": "high"})) == "high"


def test_claude_argv_precedence_end_to_end(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    envelope_input = input_factory(
        envelope=ExecutionEnvelope(effort=ReasoningEffort.HIGH),
        metadata={"reasoning_effort": "low", "effort": "medium"},
    )
    assert claude_argv_effort(fake_popen, envelope_input) == "high"

    legacy_input = input_factory(metadata={"reasoning_effort": "low", "effort": "medium"})
    assert claude_argv_effort(fake_popen, legacy_input) == "low"

    assert claude_argv_effort(fake_popen, input_factory()) is None


# ---------------------------------------------------------------------------
# Per-adapter capability differences are honoured, not faked.
# ---------------------------------------------------------------------------


def test_claude_has_no_minimal_and_maps_it_to_low(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    """``claude --help``: --effort (low, medium, high, xhigh, max). "minimal" is
    codex-only, so it is mapped DOWN to the weakest value the CLI does accept
    rather than passed through (which would be rejected) or dropped (which would
    hand back a session default stronger than what was asked for)."""
    input = input_factory(metadata={"reasoning_effort": "minimal"})
    assert cli_reasoning_effort(input, "claude") == "low"
    assert claude_argv_effort(fake_popen, input) == "low"


def test_codex_family_accepts_minimal_verbatim(input_factory: Callable[..., AgentInput]) -> None:
    input = input_factory(metadata={"effort": "minimal"})
    assert requested_reasoning_effort(input) == "minimal"
    assert cli_reasoning_effort(input, "grok") == "minimal"


def test_max_survives_to_claude_but_clamps_for_codex_family(
    fake_popen: type[FakePopen], input_factory: Callable[..., AgentInput]
) -> None:
    """ "max" is live on the intake/orchestrator path (intake/fable.py:46) and the
    Claude CLI accepts it, so it must NOT be clamped away there. codex/grok have
    no "max", so it maps to their strongest, "xhigh"."""
    input = input_factory(metadata={"effort": "max"})
    assert claude_argv_effort(fake_popen, input) == "max"
    assert requested_reasoning_effort(input) == "xhigh"
    assert cli_reasoning_effort(input, "grok") == "xhigh"


def test_providers_without_an_effort_knob_resolve_to_none(
    input_factory: Callable[..., AgentInput],
) -> None:
    """gemini, kimi, and qwen have no CLI effort flag; they must emit nothing rather
    than receive a flag their CLI would reject."""
    input = input_factory(metadata={"reasoning_effort": "xhigh"})
    assert resolve_reasoning_effort(input) == "xhigh"
    assert cli_reasoning_effort(input, "gemini") is None
    assert cli_reasoning_effort(input, "kimi") is None
    assert cli_reasoning_effort(input, "qwen") is None


@pytest.mark.parametrize("effort", list(ReasoningEffort))
def test_every_contract_effort_has_an_honest_mapping_on_every_knob(
    effort: ReasoningEffort, input_factory: Callable[..., AgentInput]
) -> None:
    """No member of the shared vocabulary may fall through the floor: each one
    must land on a value the CLI actually accepts."""
    input = input_factory(envelope=ExecutionEnvelope(effort=effort))
    for provider, vocab in (
        ("claude", {"low", "medium", "high", "xhigh", "max"}),
        ("codex", {"minimal", "low", "medium", "high", "xhigh"}),
        ("grok", {"minimal", "low", "medium", "high", "xhigh"}),
    ):
        resolved = cli_reasoning_effort(input, provider)
        assert resolved in vocab, f"{effort} -> {resolved!r} unsupported by {provider}"
