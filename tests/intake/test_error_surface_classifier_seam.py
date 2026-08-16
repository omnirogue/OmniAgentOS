"""The seam between the adapter's error STRING and the fallback CLASSIFIER.

This test exists because of a defect that neither contributing branch could see
on its own, and that no single-branch suite covers:

* ``fix/cli-state-writes-in-sandbox`` rewrote ``adapters.common.stderr_tail`` to
  return a COMPOSED HUMAN STRING (``api_error 429: You've hit your monthly spend
  limit``) instead of a sorted-JSON dump.
* ``feat/provider-resilience`` added a classifier that probes the RAW JSON
  (``"terminal_reason": "api_error"`` / ``"output_tokens": 0``).

Composed together, the classifier's probes stopped matching the very envelope
both branches were written for: the whole ``ACTION_RETRY_SAME`` rung became dead
code, and a zero-token ``api_error`` with no limit wording was judged
non-retryable — reinstating the exact "planner returns None" failure the first
branch was written to fix.

The invariant locked down here: a real provider envelope, pushed through the
REAL ``stderr_tail`` into the REAL ``_classify_attempt``, earns one same-model
retry and then advances the chain. Assert on the seam, never on either half
alone.
"""

from __future__ import annotations

import json

from omniagentos.adapters.common import STDERR_TAIL_LENGTH, stderr_tail
from omniagentos.contracts import AgentResult, AgentUsage, ResultStatus
from omniagentos.intake.fallback import (
    ACTION_ADVANCE,
    ACTION_GIVE_UP,
    ACTION_RETRY_SAME,
    _classify_attempt,
)


def _spend_limit_envelope() -> str:
    """The exact shape the claude CLI returned live on 2026-07-26."""
    return json.dumps(
        {
            "is_error": True,
            "num_turns": 1,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "terminal_reason": "api_error",
            "subtype": "success",
            "api_error_status": 429,
            "result": (
                "You've hit your monthly spend limit. Run /usage-credits to manage "
                "your limit and keep using Fable 5 or switch models to continue this chat."
            ),
            "type": "result",
        }
    )


def _internal_error_envelope() -> str:
    """A zero-token api_error with NO limit wording anywhere.

    The worst case: ``_is_limit_or_unavailable_error`` cannot rescue this one, so
    the zero-token probe is the ONLY thing standing between it and a dead planner.
    """
    return json.dumps(
        {
            "terminal_reason": "api_error",
            "subtype": "error",
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "result": "Internal server error",
        }
    )


def _as_result(error: str) -> AgentResult:
    """An AgentResult shaped the way ``CliAdapter.run`` builds one on a failure.

    Critically ``output_tokens`` is left None: ``CliAdapter._error_usage`` does
    not populate it, so the error TEXT is the only zero-token evidence available.
    """
    return AgentResult(
        status=ResultStatus.ERROR,
        error=error,
        usage=AgentUsage(wall_ms=1, estimated=True),
    )


class TestComposedErrorStillClassifies:
    def test_spend_limit_earns_a_retry_then_advances(self) -> None:
        result = _as_result(stderr_tail(_spend_limit_envelope()))
        assert _classify_attempt("claude", result, allow_retry=True) == ACTION_RETRY_SAME
        # The retry itself must not retry again — a second blip advances.
        assert _classify_attempt("claude", result, allow_retry=False) == ACTION_ADVANCE

    def test_internal_error_without_limit_wording_is_still_retryable(self) -> None:
        """The regression that would otherwise return a None plan."""
        result = _as_result(stderr_tail(_internal_error_envelope()))
        assert _classify_attempt("claude", result, allow_retry=True) == ACTION_RETRY_SAME
        assert _classify_attempt("claude", result, allow_retry=False) == ACTION_ADVANCE

    def test_the_composed_string_stays_human_readable(self) -> None:
        """The evidence suffix must not cost the diagnosis its readability."""
        text = stderr_tail(_spend_limit_envelope())
        assert text.startswith("api_error 429: You've hit your monthly spend limit")
        assert '"output_tokens": 0' in text

    def test_evidence_is_budgeted_inside_the_bound(self) -> None:
        envelope = json.dumps(
            {
                "terminal_reason": "api_error",
                "usage": {"output_tokens": 0},
                "result": "y" * (STDERR_TAIL_LENGTH * 2),
            }
        )
        text = stderr_tail(envelope)
        assert len(text) == STDERR_TAIL_LENGTH
        # Truncation must eat the message, never the machine-readable evidence.
        assert text.endswith('{"output_tokens": 0}')


class TestTheRetryRungStaysNarrow:
    def test_a_productive_api_error_is_not_a_blip(self) -> None:
        """Tokens came out, so the model WAS exercised — no free retry."""
        envelope = json.dumps(
            {
                "terminal_reason": "api_error",
                "usage": {"output_tokens": 812},
                "result": "stream interrupted",
            }
        )
        result = _as_result(stderr_tail(envelope))
        assert _classify_attempt("claude", result, allow_retry=True) == ACTION_GIVE_UP

    def test_a_bad_output_failure_is_not_a_blip(self) -> None:
        """A format break is the model's fault, not the provider's."""
        envelope = json.dumps(
            {
                "terminal_reason": "error",
                "result": "response JSON is missing required keys",
            }
        )
        result = _as_result(stderr_tail(envelope))
        assert _classify_attempt("claude", result, allow_retry=True) == ACTION_GIVE_UP

    def test_an_envelope_with_no_usage_block_earns_no_retry(self) -> None:
        """Missing evidence is not zero evidence."""
        envelope = json.dumps({"terminal_reason": "api_error", "result": "gateway blew up"})
        result = _as_result(stderr_tail(envelope))
        assert _classify_attempt("claude", result, allow_retry=True) == ACTION_GIVE_UP
