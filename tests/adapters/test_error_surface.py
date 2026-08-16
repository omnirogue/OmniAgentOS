"""A failing CLI turn must SAY why, in a form the fallback classifier can read.

Live 2026-07-26: the planner died on a claude spend-limit 429 that reached
``run_with_fallback`` as an anonymous 500-char JSON fragment. The envelope had no
``message``/``error`` key, so ``stderr_tail`` dumped the sorted JSON and kept its
LAST 500 chars — which cut off both ``api_error_status: 429`` and the human text
("You've hit your monthly spend limit"). The classifier saw no limit signal,
declared it non-retryable, and planning returned None with the next two models
untried.

The invariant these tests lock down: whatever the provider calls its fields, the
adapter's error string carries the cause, and a limit/quota cause classifies as
retryable so the chain advances.
"""

from __future__ import annotations

import json

from omniagentos.adapters.common import STDERR_TAIL_LENGTH, stderr_tail
from omniagentos.contracts import AgentResult, AgentUsage, ResultStatus
from omniagentos.intake.fallback import _is_limit_or_unavailable_error


def _spend_limit_envelope() -> str:
    """The exact shape the claude CLI returned live (fields reordered by dump)."""
    return json.dumps(
        {
            "is_error": True,
            "duration_api_ms": 0,
            "num_turns": 1,
            "stop_reason": "stop_sequence",
            "session_id": "7e832cff-0bd1-421c-8de2-8bae7b77542f",
            "total_cost_usd": 0,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
                "service_tier": "standard",
            },
            "modelUsage": {},
            "permission_denials": [],
            "terminal_reason": "api_error",
            "fast_mode_state": "off",
            "subtype": "success",
            "api_error_status": 429,
            "result": (
                "You've hit your monthly spend limit. Run /usage-credits to manage "
                "your limit and keep using Fable 5 or switch models to continue this chat."
            ),
            "type": "result",
            "uuid": "83a7d71d-a4fc-425f-87ae-7520f8545596",
        }
    )


def _as_result(error: str) -> AgentResult:
    return AgentResult(
        status=ResultStatus.ERROR, error=error, usage=AgentUsage(wall_ms=1, estimated=True)
    )


class TestStderrTailSurfacesTheCause:
    def test_spend_limit_envelope_names_status_and_reason(self) -> None:
        text = stderr_tail(_spend_limit_envelope())
        assert "429" in text
        assert "api_error" in text
        assert "monthly spend limit" in text

    def test_the_surfaced_error_classifies_as_retryable(self) -> None:
        """THE regression: this is what makes the chain advance instead of dying."""
        assert _is_limit_or_unavailable_error(_as_result(stderr_tail(_spend_limit_envelope())))

    def test_message_and_error_keys_still_win_unchanged(self) -> None:
        assert stderr_tail('{"message": "plain failure"}') == "plain failure"
        assert stderr_tail('{"error": "other failure"}') == "other failure"

    def test_non_json_stderr_is_passed_through_bounded(self) -> None:
        assert stderr_tail("  boom  ") == "boom"
        long_text = "x" * (STDERR_TAIL_LENGTH * 2)
        assert len(stderr_tail(long_text)) == STDERR_TAIL_LENGTH

    def test_envelope_without_any_message_key_falls_back_to_the_dump(self) -> None:
        text = stderr_tail('{"weird": 1, "shape": 2}')
        assert "weird" in text

    def test_success_subtype_is_not_used_as_a_prefix(self) -> None:
        """`subtype: success` on a FAILED turn is noise, not a reason."""
        text = stderr_tail('{"subtype": "success", "result": "the real cause"}')
        assert text == "the real cause"

    def test_composed_message_is_bounded(self) -> None:
        envelope = json.dumps({"api_error_status": 500, "result": "y" * (STDERR_TAIL_LENGTH * 2)})
        assert len(stderr_tail(envelope)) == STDERR_TAIL_LENGTH

    def test_a_non_limit_failure_stays_non_retryable(self) -> None:
        """The don't-waste-fallback rule is not collateral damage of this change."""
        envelope = json.dumps(
            {"terminal_reason": "error", "result": "response JSON is missing required keys"}
        )
        assert _is_limit_or_unavailable_error(_as_result(stderr_tail(envelope))) is False
