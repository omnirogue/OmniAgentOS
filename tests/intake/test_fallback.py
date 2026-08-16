"""Unit tests for the model-fallback chain (Fable → Opus → Sol).

Tests that the fallback chain correctly:
1. Tries Fable first
2. Falls back to Opus on limit/unavailable errors
3. Falls back to Sol if Opus also fails
4. Returns None if all fail
5. Distinguishes between retryable (limit) and non-retryable errors
6. Logs which model served
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from omniagentos.contracts import AgentResult, AgentUsage, ResultStatus
from omniagentos.intake import fallback as fallback_module
from omniagentos.intake.fallback import (
    _is_limit_or_unavailable_error,
    _is_zero_token_api_error,
    run_with_fallback,
)


def _make_agent_result(
    status: ResultStatus,
    output_json: dict | None = None,
    error: str | None = None,
    output_tokens: int | None = None,
) -> AgentResult:
    """Helper to create AgentResult with required usage field."""
    return AgentResult(
        status=status,
        output_json=output_json,
        error=error,
        usage=AgentUsage(wall_ms=100, turns=1, output_tokens=output_tokens, estimated=True),
    )


def _api_error_envelope(output_tokens: int = 0) -> str:
    """The EXACT error text the claude CLI produced on 2026-07-26 14:40.

    Copied from var/e2e-bench/api.log: the adapter surfaces the tail of the raw
    envelope, so it starts mid-JSON. This is the shape that used to land in the
    "giving up (not a limit/unavailable)" branch and silently degraded two swarm
    dispatches to flat solo plans."""
    return (
        ', "subtype": "success", "terminal_reason": "api_error", "total_cost_usd": 0, '
        '"type": "result", "usage": {"cache_creation": {"ephemeral_1h_input_tokens": 0, '
        '"ephemeral_5m_input_tokens": 0}, "cache_creation_input_tokens": 0, '
        '"cache_read_input_tokens": 0, "inference_geo": "", "input_tokens": 0, '
        f'"iterations": [], "output_tokens": {output_tokens}, '
        '"server_tool_use": {"web_fetch_requests": 0, "web_search_requests": 0}, '
        '"service_tier": "standard", "speed": "standard"}, '
        '"uuid": "46644c72-4540-4d48-b040-97ca2bc7c955"}'
    )


class TestLimitOrUnavailableDetection:
    """Test the error classification logic."""

    def test_ok_status_is_not_retryable(self) -> None:
        result = _make_agent_result(ResultStatus.OK, output_json={"x": 1})
        assert _is_limit_or_unavailable_error(result) is False

    def test_rate_limit_in_error_is_retryable(self) -> None:
        result = _make_agent_result(ResultStatus.ERROR, error="Rate limit exceeded")
        assert _is_limit_or_unavailable_error(result) is True

    def test_429_status_is_retryable(self) -> None:
        result = _make_agent_result(ResultStatus.ERROR, error="HTTP 429 Too Many Requests")
        assert _is_limit_or_unavailable_error(result) is True

    def test_quota_error_is_retryable(self) -> None:
        result = _make_agent_result(ResultStatus.ERROR, error="usage quota exceeded")
        assert _is_limit_or_unavailable_error(result) is True

    def test_timeout_is_retryable(self) -> None:
        result = _make_agent_result(ResultStatus.TIMEOUT, error="CLI invocation timed out")
        assert _is_limit_or_unavailable_error(result) is True

    def test_unavailable_service_is_retryable(self) -> None:
        result = _make_agent_result(ResultStatus.ERROR, error="Service temporarily unavailable")
        assert _is_limit_or_unavailable_error(result) is True

    def test_connection_refused_is_retryable(self) -> None:
        result = _make_agent_result(ResultStatus.ERROR, error="Connection refused")
        assert _is_limit_or_unavailable_error(result) is True

    def test_validation_error_is_not_retryable(self) -> None:
        result = _make_agent_result(
            ResultStatus.ERROR, error="response JSON is missing required keys"
        )
        assert _is_limit_or_unavailable_error(result) is False

    def test_malformed_json_is_not_retryable(self) -> None:
        result = _make_agent_result(ResultStatus.ERROR, error="response is not valid JSON")
        assert _is_limit_or_unavailable_error(result) is False

    def test_unknown_error_is_not_retryable(self) -> None:
        result = _make_agent_result(ResultStatus.ERROR, error="something went wrong")
        assert _is_limit_or_unavailable_error(result) is False

    def test_budget_exceeded_is_not_retryable(self) -> None:
        # BUDGET_EXCEEDED is a wallet issue, not service availability → don't retry
        result = _make_agent_result(ResultStatus.BUDGET_EXCEEDED, error="budget exceeded")
        assert _is_limit_or_unavailable_error(result) is False


class TestFallbackChain:
    """Test the model fallback chain logic."""

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_fable_success_returns_immediately(self, mock_resolve: MagicMock) -> None:
        """Fable succeeds → return its result without trying Opus/Sol."""
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        mock_adapter.run.return_value = _make_agent_result(
            ResultStatus.OK, output_json={"key": "value"}
        )

        result = run_with_fallback("test", {})
        assert result == {"key": "value"}
        # Verify only Fable was called (Claude adapter)
        assert mock_adapter.run.call_count == 1

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_fable_limit_error_tries_opus(self, mock_resolve: MagicMock) -> None:
        """Fable returns rate-limit → try Opus → return Opus result."""
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter

        # Fable fails with rate limit
        fable_result = _make_agent_result(ResultStatus.ERROR, error="Rate limit exceeded")
        # Opus succeeds
        opus_result = _make_agent_result(ResultStatus.OK, output_json={"from": "opus"})

        mock_adapter.run.side_effect = [fable_result, opus_result]

        result = run_with_fallback("test", {})
        assert result == {"from": "opus"}
        # Both Fable and Opus adapters were called
        assert mock_adapter.run.call_count == 2

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_fable_and_opus_fail_tries_sol(self, mock_resolve: MagicMock) -> None:
        """Fable and Opus both limit → try Sol → return Sol result."""
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter

        # Fable and Opus both fail with limit
        limit_result = _make_agent_result(ResultStatus.ERROR, error="quota exceeded")
        # Sol succeeds
        sol_result = _make_agent_result(ResultStatus.OK, output_json={"from": "sol"})

        mock_adapter.run.side_effect = [limit_result, limit_result, sol_result]

        result = run_with_fallback("test", {})
        assert result == {"from": "sol"}
        # All three called
        assert mock_adapter.run.call_count == 3

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_all_models_fail_returns_none(self, mock_resolve: MagicMock) -> None:
        """All models fail with limit → return None for heuristic.

        Pinned to the legacy three-rung chain: the DEFAULT chain is now eight
        rungs deep and availability-filtered per machine, so an exact call count
        only means something against an EXPLICIT chain (which is honored as
        written — see TestDefaultChain)."""
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter

        # All fail with limit errors
        limit_result = _make_agent_result(ResultStatus.ERROR, error="Rate limit")
        mock_adapter.run.side_effect = [limit_result, limit_result, limit_result]

        result = run_with_fallback("test", {}, chain="fable:opus:sol")
        assert result is None
        # All three were tried
        assert mock_adapter.run.call_count == 3

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_fable_non_limit_error_does_not_fallback(self, mock_resolve: MagicMock) -> None:
        """Fable returns non-retryable error → don't waste fallback, return None."""
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter

        # Fable fails with validation error (not retryable)
        validation_error = _make_agent_result(
            ResultStatus.ERROR, error="response JSON is missing required keys"
        )
        mock_adapter.run.return_value = validation_error

        # Explicit chain: the DEFAULT chain is availability-filtered (CLIs may
        # be absent on a hosted runner), which would degrade it to cross-lineage
        # rungs that fall through on ANY bad output instead of giving up. Pin
        # claude/codex rungs (which keep the don't-waste-fallback rule) so this
        # test is deterministic with or without the claude/codex CLIs installed.
        result = run_with_fallback("test", {}, chain="fable:opus:sol")
        assert result is None
        # Only Fable was tried; fallback was NOT used
        assert mock_adapter.run.call_count == 1

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_fable_exception_tries_opus(self, mock_resolve: MagicMock) -> None:
        """Fable adapter raises exception → try Opus."""
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter

        # Fable raises an exception
        opus_result = _make_agent_result(ResultStatus.OK, output_json={"from": "opus"})
        mock_adapter.run.side_effect = [Exception("CLI not found"), opus_result]

        result = run_with_fallback("test", {})
        assert result == {"from": "opus"}
        assert mock_adapter.run.call_count == 2

    def test_spend_guard_refusal_never_constructs_next_paid_rung(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A capped Kimi rung is terminal; OpenRouter must never be resolved."""
        from omniagentos.adapters.kimi_k3_api import FireworksKimiK3Adapter
        from omniagentos.adapters.spend_guard import SpendGuard, SpendGuardRefusal

        config = tmp_path / "spend-caps.yaml"
        config.write_text(
            "version: 1\n"
            "safety_factor: '1.25'\n"
            "soft_threshold: '0.95'\n"
            "providers:\n"
            "  kimi:\n"
            "    enabled: true\n"
            "    daily_cap_usd: '0.01'\n"
            "    models:\n"
            "      kimi-k3:\n"
            "        input_usd_per_million_tokens: '1.00'\n"
            "        output_usd_per_million_tokens: '1.00'\n"
            "        max_output_tokens: 1000\n"
            "  fireworks:\n"
            "    enabled: true\n"
            "    daily_cap_usd: '0.000001'\n"
            "    models:\n"
            "      kimi-k3:\n"
            "        input_usd_per_million_tokens: '1.00'\n"
            "        output_usd_per_million_tokens: '1.00'\n"
            "        max_output_tokens: 1000\n",
            encoding="utf-8",
        )
        guard = SpendGuard(
            config_path=config,
            db_path=str(tmp_path / "state.sqlite3"),
            alert_sender=lambda *_args, **_kwargs: type("Alert", (), {"ok": True})(),
        )
        constructed: list[str] = []

        def resolve(harness: str) -> object:
            if str(harness) == "api-kimi-k3":
                adapter = FireworksKimiK3Adapter()
                monkeypatch.setattr(adapter, "spend_guard", lambda: guard)
                monkeypatch.setattr(adapter, "api_key", lambda: "fireworks-test-key")
                return adapter
            constructed.append(str(harness))
            raise AssertionError("OpenRouter adapter was constructed after spend refusal")

        monkeypatch.setattr("omniagentos.adapters.registry.resolve_adapter", resolve)
        monkeypatch.setattr(
            "requests.post",
            lambda *_args, **_kwargs: pytest.fail("cap refusal reached HTTP"),
        )
        try:
            with pytest.raises(SpendGuardRefusal) as caught:
                run_with_fallback(
                    "plan something",
                    {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                    chain=("kimi-k3-api", "openrouter"),
                )
            assert caught.value.reason_class == "daily_cap_exceeded"
            assert constructed == []
        finally:
            guard.close()

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_effort_passed_to_fable_only(self, mock_resolve: MagicMock) -> None:
        """Effort parameter is only applied to Fable (Claude), not Opus/Sol."""
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter

        # Fable fails with limit, Opus succeeds
        limit_result = _make_agent_result(ResultStatus.ERROR, error="Rate limit")
        opus_result = _make_agent_result(ResultStatus.OK, output_json={"result": "ok"})
        mock_adapter.run.side_effect = [limit_result, opus_result]

        # Explicit two-rung chain: the DEFAULT chain is availability-filtered
        # (CLIs may be absent on a hosted runner) and can degrade to rungs
        # other than fable/opus, breaking the fixed 2-item side_effect list and
        # the per-rung assertions below. Pin it so this is deterministic.
        run_with_fallback("test", {}, effort="max", chain="fable:opus")

        # Check the calls
        calls = mock_adapter.run.call_args_list
        assert len(calls) == 2

        # First call (Fable) should have effort in metadata
        fable_input = calls[0][0][0]
        assert fable_input.metadata.get("effort") == "max"

        # Second call (Opus) should NOT have effort in metadata
        opus_input = calls[1][0][0]
        assert opus_input.metadata.get("effort") is None

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_fable_empty_json_response_does_not_fallback(self, mock_resolve: MagicMock) -> None:
        """Fable returns error (not retryable) → don't fall back."""
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter

        # Fable returns error without JSON (validation error case)
        empty_error = _make_agent_result(
            ResultStatus.ERROR, error="response JSON is missing required keys", output_json=None
        )
        mock_adapter.run.return_value = empty_error

        # Explicit chain: see test_fable_non_limit_error_does_not_fallback for
        # why the default (availability-filtered) chain is nondeterministic here.
        result = run_with_fallback("test", {}, chain="fable:opus:sol")
        # Non-retryable error → should NOT fall back, return None
        assert result is None
        assert mock_adapter.run.call_count == 1  # Only Fable was tried


class TestLogging:
    """Test that the fallback chain logs which model served."""

    @patch("omniagentos.intake.fallback.LOG")
    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_logs_which_model_served(self, mock_resolve: MagicMock, mock_log: MagicMock) -> None:
        """Verify logging indicates which model served the result."""
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter

        # Fable fails, Opus succeeds
        limit_result = _make_agent_result(ResultStatus.ERROR, error="Rate limit")
        opus_result = _make_agent_result(ResultStatus.OK, output_json={"result": "ok"})
        mock_adapter.run.side_effect = [limit_result, opus_result]

        # Explicit two-rung chain: see test_effort_passed_to_fable_only for why
        # the default (availability-filtered) chain is nondeterministic here.
        run_with_fallback("test", {}, effort="high", chain="fable:opus")

        # Check that LOG.info was called with "Model opus served"
        logged_messages = [call[0][0] for call in mock_log.info.call_args_list]
        assert any("opus" in msg and "served" in msg for msg in logged_messages)

    @patch("omniagentos.intake.fallback.LOG")
    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_logs_fallback_attempts(self, mock_resolve: MagicMock, mock_log: MagicMock) -> None:
        """Verify logging shows fallback attempts."""
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter

        limit_result = _make_agent_result(ResultStatus.ERROR, error="Rate limit")
        mock_adapter.run.return_value = limit_result

        run_with_fallback("test", {})

        # Check that LOG.warning was called about fallback attempts
        logged_messages = [call[0][0] for call in mock_log.warning.call_args_list]
        assert any("trying fallback" in msg.lower() for msg in logged_messages)

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_non_retryable_error_is_visible_at_warning(
        self, mock_resolve: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        mock_adapter.run.return_value = _make_agent_result(
            ResultStatus.ERROR,
            error="error: unknown option '--effort'",
        )

        # Explicit chain: see test_fable_non_limit_error_does_not_fallback for
        # why the default (availability-filtered) chain is nondeterministic here.
        with caplog.at_level(logging.WARNING, logger="omniagentos.intake.fallback"):
            result = run_with_fallback("test", {}, chain="fable:opus:sol")

        assert result is None
        assert mock_adapter.run.call_count == 1
        assert any(
            record.levelno >= logging.WARNING and "unknown option '--effort'" in record.getMessage()
            for record in caplog.records
        )


def test_api_error_backoff_is_a_short_wait() -> None:
    """The shipped backoff stays in the 2-5s band the brief specifies."""
    assert 2 <= fallback_module.API_ERROR_RETRY_BACKOFF_SECONDS <= 5


class TestZeroTokenApiErrorIsRetryable:
    """Regression for the live 2026-07-26 14:40 degradation.

    A terminal `api_error` with ZERO output tokens is a PROVIDER blip: nothing
    was produced, so nothing about the prompt was exercised. It must earn one
    short same-model retry and then advance the chain -- never the
    "giving up (not a limit/unavailable)" branch that silently degraded two
    swarm dispatches to flat solo plans.
    """

    @pytest.fixture(autouse=True)
    def _no_real_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fallback_module, "API_ERROR_RETRY_BACKOFF_SECONDS", 0)

    def test_classifier_flags_the_observed_shape(self) -> None:
        result = _make_agent_result(ResultStatus.ERROR, error=_api_error_envelope())
        assert _is_zero_token_api_error(result) is True
        # ...and the OLD classifier still says "not a limit/unavailable", which
        # is exactly why the new one has to exist.
        assert _is_limit_or_unavailable_error(result) is False

    def test_classifier_ignores_an_api_error_that_produced_output(self) -> None:
        result = _make_agent_result(
            ResultStatus.ERROR, error=_api_error_envelope(output_tokens=412)
        )
        assert _is_zero_token_api_error(result) is False

    def test_classifier_ignores_a_reported_positive_token_count(self) -> None:
        result = _make_agent_result(
            ResultStatus.ERROR, error=_api_error_envelope(), output_tokens=99
        )
        assert _is_zero_token_api_error(result) is False

    def test_classifier_ignores_other_errors(self) -> None:
        result = _make_agent_result(
            ResultStatus.ERROR, error="response JSON is missing required keys"
        )
        assert _is_zero_token_api_error(result) is False

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_retried_on_the_same_model_then_falls_back(self, mock_resolve: MagicMock) -> None:
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        api_error = _make_agent_result(ResultStatus.ERROR, error=_api_error_envelope())
        opus_ok = _make_agent_result(ResultStatus.OK, output_json={"from": "opus"})
        # fable blips, the same-model retry blips again, THEN opus serves.
        mock_adapter.run.side_effect = [api_error, api_error, opus_ok]

        result = run_with_fallback("test", {}, chain="fable:opus")

        assert result == {"from": "opus"}
        assert mock_adapter.run.call_count == 3
        models = [call[0][0].model for call in mock_adapter.run.call_args_list]
        assert models == ["fable", "fable", "opus"]

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_the_same_model_retry_can_serve_the_plan(self, mock_resolve: MagicMock) -> None:
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        mock_adapter.run.side_effect = [
            _make_agent_result(ResultStatus.ERROR, error=_api_error_envelope()),
            _make_agent_result(ResultStatus.OK, output_json={"from": "fable"}),
        ]

        result = run_with_fallback("test", {}, chain="fable:opus")

        assert result == {"from": "fable"}
        assert mock_adapter.run.call_count == 2

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_the_backoff_is_actually_slept(self, mock_resolve: MagicMock) -> None:
        """The retry waits, it does not hammer the provider."""
        slept: list[float] = []
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        mock_adapter.run.side_effect = [
            _make_agent_result(ResultStatus.ERROR, error=_api_error_envelope()),
            _make_agent_result(ResultStatus.OK, output_json={"ok": 1}),
        ]
        with patch.object(fallback_module.time, "sleep", slept.append):
            run_with_fallback("test", {}, chain="fable")

        assert slept == [0]  # the fixture pinned the constant; the call happened

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_a_blipping_provider_costs_two_attempts_not_the_whole_chain(
        self, mock_resolve: MagicMock
    ) -> None:
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        mock_adapter.run.return_value = _make_agent_result(
            ResultStatus.ERROR, error=_api_error_envelope()
        )

        result = run_with_fallback("test", {}, chain="fable:opus:sol")

        assert result is None
        assert mock_adapter.run.call_count == 6  # 3 rungs x (attempt + 1 retry)


def _truncated_api_error_envelope() -> str:
    """The same terminal, cut off ABOVE the usage block.

    The adapters surface only the TAIL of the raw envelope, so this shape is
    routine — and it says NOTHING about how many tokens the model produced.
    """
    return (
        ', "subtype": "success", "terminal_reason": "api_error", '
        '"total_cost_usd": 0, "type": "result"'
    )


class TestZeroTokensMustBeShownNotAssumed:
    """REGRESSION (critic finding 6). Missing usage data is not zero usage.

    `_is_zero_token_api_error` used to end in `not <positive-token-regex>`, so an
    envelope with NO usage block at all — a truncation, which is the normal case
    for a tail-captured envelope — counted as "the model produced nothing" and
    bought the provider a same-model retry it never earned. Zero now has to be
    SHOWN: an explicit `usage.output_tokens == 0` or an explicit
    `"output_tokens": 0` in the text.
    """

    def test_a_truncated_envelope_is_undecidable_not_zero(self) -> None:
        result = _make_agent_result(ResultStatus.ERROR, error=_truncated_api_error_envelope())
        assert _is_zero_token_api_error(result) is False

    def test_an_explicit_zero_in_the_envelope_still_counts(self) -> None:
        result = _make_agent_result(ResultStatus.ERROR, error=_api_error_envelope())
        assert _is_zero_token_api_error(result) is True

    def test_an_explicit_structured_zero_counts_without_envelope_evidence(self) -> None:
        result = _make_agent_result(
            ResultStatus.ERROR, error=_truncated_api_error_envelope(), output_tokens=0
        )
        assert _is_zero_token_api_error(result) is True

    def test_a_ten_token_envelope_is_not_mistaken_for_zero(self) -> None:
        """`"output_tokens": 10` must not match the explicit-zero probe."""
        result = _make_agent_result(ResultStatus.ERROR, error=_api_error_envelope(output_tokens=10))
        assert _is_zero_token_api_error(result) is False

    def test_a_positive_structured_count_beats_an_envelope_zero(self) -> None:
        result = _make_agent_result(
            ResultStatus.ERROR, error=_api_error_envelope(), output_tokens=7
        )
        assert _is_zero_token_api_error(result) is False

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_a_truncated_envelope_does_not_buy_a_retry(self, mock_resolve: MagicMock) -> None:
        """The behavioral half: no same-model retry on undecidable evidence."""
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        mock_adapter.run.return_value = _make_agent_result(
            ResultStatus.ERROR, error=_truncated_api_error_envelope()
        )

        with patch.object(fallback_module.time, "sleep") as slept:
            result = run_with_fallback("test", {}, chain="fable:opus")

        assert result is None
        assert mock_adapter.run.call_count == 1  # attempted once, never retried
        slept.assert_not_called()


class TestTheRetryIsClassifiedLikeAnyOtherAttempt:
    """REGRESSION (critic finding 7). The retry result was never re-classified.

    After the same-model retry the old loop just `continue`d, so ANY retry
    failure advanced the chain — including the ones the chain's own rules say
    should end planning (a claude/codex rung that "ran fine but gave a bad
    answer" must not spend a fallback). Both attempts now go through the same
    `_classify_attempt`; the retry differs only in that it cannot earn a second
    retry.
    """

    @pytest.fixture(autouse=True)
    def _no_real_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fallback_module, "API_ERROR_RETRY_BACKOFF_SECONDS", 0)

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_a_non_retryable_failure_on_the_retry_stops_the_chain(
        self, mock_resolve: MagicMock
    ) -> None:
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        mock_adapter.run.side_effect = [
            _make_agent_result(ResultStatus.ERROR, error=_api_error_envelope()),
            _make_agent_result(ResultStatus.ERROR, error="error: unknown option '--effort'"),
            _make_agent_result(ResultStatus.OK, output_json={"from": "opus"}),
        ]

        result = run_with_fallback("test", {}, chain="fable:opus")

        assert result is None  # the same verdict the FIRST attempt would have got
        assert mock_adapter.run.call_count == 2  # opus was never asked

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_an_invalid_model_failure_on_the_retry_stops_the_chain(
        self, mock_resolve: MagicMock
    ) -> None:
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        mock_adapter.run.side_effect = [
            _make_agent_result(ResultStatus.ERROR, error=_api_error_envelope()),
            _make_agent_result(ResultStatus.ERROR, error="invalid model 'fable'"),
            _make_agent_result(ResultStatus.OK, output_json={"from": "opus"}),
        ]

        assert run_with_fallback("test", {}, chain="fable:opus") is None
        assert mock_adapter.run.call_count == 2

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_a_limit_failure_on_the_retry_still_advances(self, mock_resolve: MagicMock) -> None:
        """Same classifier means the retryable classes keep working."""
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        mock_adapter.run.side_effect = [
            _make_agent_result(ResultStatus.ERROR, error=_api_error_envelope()),
            _make_agent_result(ResultStatus.ERROR, error="429 Too Many Requests"),
            _make_agent_result(ResultStatus.OK, output_json={"from": "opus"}),
        ]

        assert run_with_fallback("test", {}, chain="fable:opus") == {"from": "opus"}
        assert mock_adapter.run.call_count == 3

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_a_bad_answer_on_a_cross_lineage_rung_retry_still_falls_through(
        self, mock_resolve: MagicMock
    ) -> None:
        """The fall-through rule is part of the shared classifier, not the loop."""
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        mock_adapter.run.side_effect = [
            _make_agent_result(ResultStatus.ERROR, error=_api_error_envelope()),
            _make_agent_result(ResultStatus.ERROR, error="response JSON is missing required keys"),
            _make_agent_result(ResultStatus.OK, output_json={"from": "opus"}),
        ]

        assert run_with_fallback("test", {}, chain="grok:opus") == {"from": "opus"}
        assert mock_adapter.run.call_count == 3

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_the_retry_never_earns_a_second_retry(self, mock_resolve: MagicMock) -> None:
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        mock_adapter.run.return_value = _make_agent_result(
            ResultStatus.ERROR, error=_api_error_envelope()
        )

        assert run_with_fallback("test", {}, chain="fable:opus") is None
        assert mock_adapter.run.call_count == 4  # 2 rungs x (attempt + 1 retry)

    def test_the_classifier_is_one_function_used_for_both_attempts(self) -> None:
        """A second zero-token blip advances instead of retrying forever."""
        blip = _make_agent_result(ResultStatus.ERROR, error=_api_error_envelope())
        assert (
            fallback_module._classify_attempt("fable", blip, allow_retry=True)
            == fallback_module.ACTION_RETRY_SAME
        )
        assert (
            fallback_module._classify_attempt("fable", blip, allow_retry=False)
            == fallback_module.ACTION_ADVANCE
        )
        bad = _make_agent_result(ResultStatus.ERROR, error="malformed JSON")
        assert (
            fallback_module._classify_attempt("fable", bad, allow_retry=False)
            == fallback_module.ACTION_GIVE_UP
        )
        assert (
            fallback_module._classify_attempt("grok", bad, allow_retry=False)
            == fallback_module.ACTION_ADVANCE
        )


class TestDefaultChain:
    """Deliverable 2: an 8-rung cross-provider default, filtered to the machine."""

    @pytest.fixture
    def all_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every CLI present, every provider enabled, every api rung configured."""
        import shutil as shutil_module

        monkeypatch.setattr(shutil_module, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(fallback_module, "_cli_available", lambda cli: True)
        monkeypatch.setattr(fallback_module, "_provider_enabled", lambda provider: True)
        monkeypatch.setenv("FIREWORKS_API_KEY", "fw-test")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.delenv("OMNIAGENTOS_PLANNER_FALLBACKS", raising=False)

    def test_full_chain_is_eight_cross_provider_rungs(self, all_available: None) -> None:
        rungs = fallback_module.default_chain_rungs()

        assert [rung.name for rung in rungs] == [
            "fable",
            "opus",
            "sol",
            "grok",
            "gemini-flash-api",
            "kimi-k3-api",
            "gemini-lite-api",
            "openrouter",
        ]
        assert len(rungs) == fallback_module.MAX_CHAIN_RUNGS
        # Cross-provider by construction: the subscription CLIs plus API tier.
        assert {str(rung.harness) for rung in rungs} == {
            "cli-claude",
            "cli-codex",
            "cli-grok",
            "api-kimi-k3",
            "api-litellm",
            "api-openrouter",
        }
        # The api rungs carry a policy path; the CLI rungs do not.
        assert [rung.name for rung in rungs if rung.api_path] == [
            "gemini-flash-api",
            "kimi-k3-api",
            "gemini-lite-api",
            "openrouter",
        ]

    def test_chain_is_capped_at_eight_rungs(
        self, all_available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fallback_module, "DEFAULT_CHAIN", ("fable",) * 12)
        assert len(fallback_module.default_chain_rungs()) == fallback_module.MAX_CHAIN_RUNGS

    def test_missing_cli_binaries_are_skipped(
        self, all_available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shutil as shutil_module

        monkeypatch.undo()  # drop the blanket _cli_available stub
        monkeypatch.setenv("FIREWORKS_API_KEY", "fw-test")
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.delenv("OMNIAGENTOS_PLANNER_FALLBACKS", raising=False)
        monkeypatch.setattr(fallback_module, "_provider_enabled", lambda provider: True)
        monkeypatch.setattr(
            shutil_module,
            "which",
            lambda name: None if name in {"grok", "kimi"} else f"/usr/bin/{name}",
        )

        names = [rung.name for rung in fallback_module.default_chain_rungs()]

        assert "grok" not in names
        assert "kimi" not in names
        assert "kimi-k3-api" in names  # Direct Fireworks does not need the Kimi CLI.
        # ...and the rest of the chain, api tier included, still stands.
        assert names == [
            "fable",
            "opus",
            "sol",
            "gemini-flash-api",
            "kimi-k3-api",
            "gemini-lite-api",
            "openrouter",
        ]

    def test_missing_claude_cli_skips_both_claude_rungs(
        self, all_available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fallback_module, "_cli_available", lambda cli: cli != "claude")

        names = [rung.name for rung in fallback_module.default_chain_rungs()]

        assert "fable" not in names and "opus" not in names
        assert names[0] == "sol"

    def test_disabled_provider_accounts_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No `all_available` stub here: this exercises the REAL account probe."""
        import shutil as shutil_module

        from omniagentos.routing import config as accounts_config

        monkeypatch.delenv("OMNIAGENTOS_PLANNER_FALLBACKS", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setattr(shutil_module, "which", lambda name: f"/usr/bin/{name}")
        disabled = accounts_config.AccountPoolConfig(
            providers={
                "codex": accounts_config.ProviderPool(
                    accounts=[
                        accounts_config.Account(
                            id="codex-default", config_dir="~/.codex", enabled=False
                        )
                    ]
                )
            }
        )
        monkeypatch.setattr(accounts_config, "load_accounts_config", lambda *a, **k: disabled)

        names = [rung.name for rung in fallback_module.default_chain_rungs()]

        assert "sol" not in names  # every codex account is disabled
        assert "fable" in names  # a provider absent from the pool config still runs

    def test_unparseable_accounts_config_is_not_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REQUIREMENT: unparseable accounts source must never render as enabled.

        Counterfeit: except-and-return-True (chain-widen on config trouble) —
        that presents a measurement failure as favourable provider enablement.
        The probe returns False so availability filtering drops the provider.
        """
        from omniagentos.routing import config as accounts_config

        def _boom(*_a: object, **_k: object) -> object:
            raise ValueError("unparseable accounts config")

        monkeypatch.setattr(accounts_config, "load_accounts_config", _boom)

        assert fallback_module._provider_enabled("codex") is False

    def test_unparseable_accounts_config_drops_provider_from_default_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unparseable accounts must not admit the provider into the filtered chain."""
        import shutil as shutil_module

        from omniagentos.routing import config as accounts_config

        monkeypatch.delenv("OMNIAGENTOS_PLANNER_FALLBACKS", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setattr(shutil_module, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(fallback_module, "_cli_available", lambda cli: True)

        def _boom(*_a: object, **_k: object) -> object:
            raise ValueError("unparseable accounts config")

        monkeypatch.setattr(accounts_config, "load_accounts_config", _boom)

        # Isolate accounts-source verdict: CLI held available (as reviewer probe).
        sol = fallback_module._RUNGS["sol"]
        assert sol.provider == "codex"
        assert fallback_module._provider_enabled("codex") is False
        assert fallback_module._rung_available(sol) is False
        names = [rung.name for rung in fallback_module.default_chain_rungs()]
        assert "sol" not in names

    def test_openrouter_rung_needs_a_key(
        self, all_available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        names = [rung.name for rung in fallback_module.default_chain_rungs()]

        assert "openrouter" not in names
        assert len(names) == 7  # the rest of the chain is untouched

    def test_openrouter_rung_binds_the_first_configured_model(self, all_available: None) -> None:
        from omniagentos.routing.api_policy import openrouter_models

        rung = next(r for r in fallback_module.default_chain_rungs() if r.name == "openrouter")
        assert rung.model == openrouter_models()[0]

    def test_env_override_is_honored_exactly_as_written(
        self, all_available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMNIAGENTOS_PLANNER_FALLBACKS is an operator decision, not a hint."""
        monkeypatch.setenv("OMNIAGENTOS_PLANNER_FALLBACKS", "sol:fable")
        # ...even when the availability probe would have dropped a rung.
        monkeypatch.setattr(fallback_module, "_cli_available", lambda cli: False)

        rungs = fallback_module._resolve_chain(None)

        assert [rung.name for rung in rungs] == ["sol", "fable"]

    def test_per_call_chain_still_wins_over_the_env(
        self, all_available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMNIAGENTOS_PLANNER_FALLBACKS", "sol:fable")

        rungs = fallback_module._resolve_chain("gemini:fable")

        assert [rung.name for rung in rungs] == ["gemini", "fable"]

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_the_default_chain_is_what_run_with_fallback_walks(
        self, mock_resolve: MagicMock, all_available: None
    ) -> None:
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        mock_adapter.run.return_value = _make_agent_result(
            ResultStatus.ERROR, error="Rate limit exceeded"
        )

        assert run_with_fallback("test", {}) is None
        assert mock_adapter.run.call_count == 8

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_cross_lineage_rungs_fall_through_on_a_bad_answer(
        self, mock_resolve: MagicMock, all_available: None
    ) -> None:
        """A format break on grok/kimi/api must NOT end planning while rungs remain."""
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        bad_output = _make_agent_result(
            ResultStatus.ERROR, error="response JSON is missing required keys"
        )
        served = _make_agent_result(ResultStatus.OK, output_json={"from": "openrouter"})
        mock_adapter.run.side_effect = [bad_output, served]

        result = run_with_fallback("test", {}, chain="grok:openrouter")

        assert result == {"from": "openrouter"}
        assert mock_adapter.run.call_count == 2


class TestApiPolicyIsEnforcedAtChainBuild:
    """The deny-list fails CLOSED, and the run loop never swallows it."""

    def test_a_claude_model_on_an_api_harness_raises(self) -> None:
        from omniagentos.routing.api_policy import ApiRoutePolicyError

        with pytest.raises(ApiRoutePolicyError):
            run_with_fallback("test", {}, chain=[("bad", "api-openrouter", "claude-opus-5")])

    def test_a_gpt_model_on_an_api_harness_raises(self) -> None:
        from omniagentos.routing.api_policy import ApiRoutePolicyError

        with pytest.raises(ApiRoutePolicyError):
            run_with_fallback("test", {}, chain=[("bad", "api-litellm", "gpt-5.6-sol")])

    def test_a_denied_configured_openrouter_model_breaks_the_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import yaml

        from omniagentos.routing.api_policy import ApiRoutePolicyError

        config = tmp_path / "swarm.yaml"
        config.write_text(
            yaml.safe_dump({"api_fallback": {"openrouter_models": ["anthropic/claude-opus-5"]}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("OMNIAGENTOS_SWARM_CONFIG", str(config))
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

        with pytest.raises(ApiRoutePolicyError):
            fallback_module.default_chain_rungs()

    def test_gemini_on_the_litellm_path_is_allowed(self) -> None:
        rungs = fallback_module._resolve_chain("gemini-flash-api:gemini-lite-api")
        assert [rung.model for rung in rungs] == [
            fallback_module.FAST_PLANNER_MODEL,
            fallback_module.LITE_PLANNER_MODEL,
        ]
        assert all(rung.api_path == "litellm" for rung in rungs)
