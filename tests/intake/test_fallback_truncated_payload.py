"""Regression tests for O-7: unparseable CLI payload advances the fallback chain.

When adapters.common.stderr_tail surfaces a 500-char tail of a sorted JSON
envelope (no usable message key), ``result.error`` becomes a mid-key slice of
CLI machine telemetry — not a coherent model error. That is an infrastructure
failure: the chain must ADVANCE, not give up with healthy rungs untried.

These tests lock the predicate and the behavioural claim. They must fail if
the classifier reverts to ACTION_GIVE_UP on the observed production fragment.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from omniagentos.adapters.common import stderr_tail
from omniagentos.contracts import AgentResult, AgentUsage, ResultStatus
from omniagentos.intake.fallback import (
    ACTION_ADVANCE,
    ACTION_GIVE_UP,
    _classify_attempt,
    _count_cli_telemetry_keys,
    _is_limit_or_unavailable_error,
    _is_raw_or_truncated_json_dump,
    _is_unparseable_cli_payload,
    run_with_fallback,
)

# VERBATIM observed production fragment (result.error on the opus rung).
# Begins mid-key inside cache_read_input_tokens → ``ut_tokens": ...``.
OBSERVED_TRUNCATED_CLI_PAYLOAD = (
    'ut_tokens": 70850, "inference_geo": "not_available", "input_tokens": 6, '
    '"iterations": [{"cache_creation": {...}}], "output_tokens": 1262, ... '
    '"uuid": "f1d5..."'
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


class TestIsUnparseableCliPayload:
    """Unit tests for the truncated-CLI-payload predicate."""

    def test_observed_production_fragment_is_unparseable(self) -> None:
        """(a) The verbatim production fragment must classify as infrastructure."""
        result = _make_agent_result(ResultStatus.ERROR, error=OBSERVED_TRUNCATED_CLI_PAYLOAD)
        assert _is_unparseable_cli_payload(result) is True
        assert _classify_attempt("opus", result, allow_retry=True) == ACTION_ADVANCE

    def test_ok_status_is_never_unparseable(self) -> None:
        result = _make_agent_result(
            ResultStatus.OK,
            output_json={"plan": "x"},
            error=OBSERVED_TRUNCATED_CLI_PAYLOAD,
        )
        assert _is_unparseable_cli_payload(result) is False

    def test_timeout_status_with_fragment_is_unparseable(self) -> None:
        result = _make_agent_result(ResultStatus.TIMEOUT, error=OBSERVED_TRUNCATED_CLI_PAYLOAD)
        assert _is_unparseable_cli_payload(result) is True


class TestChainAdvancesOnTruncatedPayload:
    """(b) Behavioural: truncated CLI payload must advance fable → opus."""

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_truncated_payload_advances_to_opus(self, mock_resolve: MagicMock) -> None:
        """Core claim: fable ERROR with the fragment → opus serves a plan.

        If the fix is reverted, the classifier returns ACTION_GIVE_UP on the
        first rung and this test fails (adapter called once, result is None).
        """
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter

        fable_result = _make_agent_result(
            ResultStatus.ERROR, error=OBSERVED_TRUNCATED_CLI_PAYLOAD
        )
        opus_result = _make_agent_result(
            ResultStatus.OK, output_json={"plan": "from-opus", "tasks": []}
        )
        mock_adapter.run.side_effect = [fable_result, opus_result]

        result = run_with_fallback("test", {}, chain="fable:opus")
        assert result == {"plan": "from-opus", "tasks": []}
        assert mock_adapter.run.call_count == 2


class TestGenuineErrorsStillGiveUp:
    """(c) Genuine non-retryable model errors must still stop the chain."""

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def _assert_gives_up(self, error: str, mock_resolve: MagicMock) -> None:
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        err_result = _make_agent_result(ResultStatus.ERROR, error=error)
        mock_adapter.run.return_value = err_result

        assert _is_unparseable_cli_payload(err_result) is False
        assert _classify_attempt("fable", err_result, allow_retry=True) == ACTION_GIVE_UP

        result = run_with_fallback("test", {}, chain="fable:opus")
        assert result is None
        assert mock_adapter.run.call_count == 1

    def test_missing_required_keys(self) -> None:
        self._assert_gives_up("response JSON is missing required keys: tasks, plan")

    def test_schema_validation_error(self) -> None:
        self._assert_gives_up(
            "schema validation failed: 'tasks' is a required property"
        )

    def test_invalid_model_error(self) -> None:
        self._assert_gives_up("invalid model: claude-unknown-xyz is not supported")

    def test_plain_model_refusal_prose(self) -> None:
        self._assert_gives_up(
            "I cannot help with that request because it violates my usage policy."
        )

    def test_composed_stderr_tail_with_output_tokens_suffix(self) -> None:
        """Trap: stderr_tail appends output_tokens to real composed errors.

        Counting output_tokens as a telemetry key would misclassify this as
        infrastructure and wrongly advance the chain.
        """
        composed = 'api_error 400: I cannot help with that request {"output_tokens": 42}'
        result = _make_agent_result(ResultStatus.ERROR, error=composed)
        assert _is_unparseable_cli_payload(result) is False
        assert _classify_attempt("fable", result, allow_retry=True) == ACTION_GIVE_UP
        self._assert_gives_up(composed)


class TestExistingOwnersStillOwnAuthAndLimits:
    """(d) Auth / limit errors still advance via the EXISTING classifier."""

    def test_rate_limit_owned_by_existing_predicate(self) -> None:
        result = _make_agent_result(ResultStatus.ERROR, error="Rate limit exceeded")
        assert _is_limit_or_unavailable_error(result) is True
        assert _is_unparseable_cli_payload(result) is False
        assert _classify_attempt("fable", result, allow_retry=True) == ACTION_ADVANCE

    def test_auth_401_owned_by_existing_predicate(self) -> None:
        result = _make_agent_result(ResultStatus.ERROR, error="401 Unauthorized")
        assert _is_limit_or_unavailable_error(result) is True
        assert _is_unparseable_cli_payload(result) is False
        assert _classify_attempt("fable", result, allow_retry=True) == ACTION_ADVANCE

    def test_auth_forbidden_owned_by_existing_predicate(self) -> None:
        result = _make_agent_result(ResultStatus.ERROR, error="403 Forbidden: authentication failed")
        assert _is_limit_or_unavailable_error(result) is True
        assert _is_unparseable_cli_payload(result) is False
        assert _classify_attempt("fable", result, allow_retry=True) == ACTION_ADVANCE

    def test_quota_owned_by_existing_predicate(self) -> None:
        result = _make_agent_result(ResultStatus.ERROR, error="usage quota exceeded")
        assert _is_limit_or_unavailable_error(result) is True
        assert _is_unparseable_cli_payload(result) is False
        assert _classify_attempt("fable", result, allow_retry=True) == ACTION_ADVANCE

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_auth_failure_still_advances_chain(self, mock_resolve: MagicMock) -> None:
        """Behavioural: existing limit/auth path still advances (not swallowed)."""
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        auth_result = _make_agent_result(ResultStatus.ERROR, error="401 Unauthorized")
        opus_result = _make_agent_result(ResultStatus.OK, output_json={"plan": "ok"})
        mock_adapter.run.side_effect = [auth_result, opus_result]

        result = run_with_fallback("test", {}, chain="fable:opus")
        assert result == {"plan": "ok"}
        assert mock_adapter.run.call_count == 2


class TestUnparseableLogging:
    """Ops must see a distinguishable warning for CLI garbage vs model errors."""

    @patch("omniagentos.intake.fallback.LOG")
    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_logs_unparseable_cli_infrastructure(
        self, mock_resolve: MagicMock, mock_log: MagicMock
    ) -> None:
        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        fable_result = _make_agent_result(
            ResultStatus.ERROR, error=OBSERVED_TRUNCATED_CLI_PAYLOAD
        )
        opus_result = _make_agent_result(ResultStatus.OK, output_json={"plan": "ok"})
        mock_adapter.run.side_effect = [fable_result, opus_result]

        run_with_fallback("test", {}, chain="fable:opus")

        warned = [str(call[0][0]) for call in mock_log.warning.call_args_list]
        assert any(
            "unparseable CLI" in msg and "infrastructure" in msg and "advancing" in msg
            for msg in warned
        ), f"expected infrastructure-advance log, got: {warned}"


class TestGateAIsLoadBearing:
    """Gate A must be load-bearing — realistic model-output false positives.

    These cases pass Gate B (truncated/unparseable JSON density, or parseable
    dict in the error slot) and are stopped ONLY by Gate A, because they use
    the model's vocabulary, not CLI telemetry keys.

    This test class must FAIL if Gate A
    (``_count_cli_telemetry_keys(text) < 2``) is removed.
    """

    # Truncated, malformed model plan — "ran fine but gave a bad answer".
    # Passes Gate B via B2 (>=3 key separators, unparseable). Stopped only by A.
    TRUNCATED_MODEL_PLAN = '{"tasks": [{"id": 1, "title": "do x"}, {"id": 2, "title": "do'

    # Well-formed but schema-wrong JSON sitting in the error slot.
    # Passes Gate B via B3 (parseable dict). Stopped only by A.
    SCHEMA_WRONG_JSON = '{"tasks": [], "summary": "nothing to do", "notes": "n/a"}'

    def _assert_gate_a_only_stop(self, error: str) -> None:
        """Error must clear Gate B but fail Gate A, and the chain must stop."""
        # Isolation evidence: Gate B alone would accept; Gate A rejects.
        assert _count_cli_telemetry_keys(error) < 2
        assert _is_raw_or_truncated_json_dump(error) is True

        result = _make_agent_result(ResultStatus.ERROR, error=error)
        assert _is_unparseable_cli_payload(result) is False
        assert _classify_attempt("fable", result, allow_retry=True) == ACTION_GIVE_UP

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_truncated_malformed_model_plan_gives_up(
        self, mock_resolve: MagicMock
    ) -> None:
        """Asking Opus does not fix Fable's malformed JSON — stop the chain."""
        self._assert_gate_a_only_stop(self.TRUNCATED_MODEL_PLAN)

        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        mock_adapter.run.return_value = _make_agent_result(
            ResultStatus.ERROR, error=self.TRUNCATED_MODEL_PLAN
        )

        result = run_with_fallback("test", {}, chain="fable:opus")
        assert result is None
        assert mock_adapter.run.call_count == 1

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_schema_wrong_json_in_error_slot_gives_up(
        self, mock_resolve: MagicMock
    ) -> None:
        """Parseable but schema-wrong plan JSON is a model fault, not CLI dump."""
        self._assert_gate_a_only_stop(self.SCHEMA_WRONG_JSON)

        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        mock_adapter.run.return_value = _make_agent_result(
            ResultStatus.ERROR, error=self.SCHEMA_WRONG_JSON
        )

        result = run_with_fallback("test", {}, chain="fable:opus")
        assert result is None
        assert mock_adapter.run.call_count == 1


class TestGateBIsLoadBearing:
    """Gate B must be load-bearing — prose that names telemetry keys.

    A composed human error that legitimately mentions two or more telemetry
    keys in JSON-key position clears Gate A and is stopped ONLY by Gate B
    (prose: not mid-key, fewer than 3 key separators, not parseable JSON).

    This test class must FAIL if Gate B
    (``return _is_raw_or_truncated_json_dump(text)``) is removed / short-circuited
    to always True.
    """

    # Clears Gate A: "input_tokens" and "num_turns" in `"key":` form.
    # Rejected by Gate B: prose, 2 key separators only, not parseable JSON.
    COMPOSED_TELEMETRY_PROSE = (
        'api_error 400: your request set "input_tokens": 6 but "num_turns": 2 is invalid'
    )

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_composed_prose_with_telemetry_keys_gives_up(
        self, mock_resolve: MagicMock
    ) -> None:
        error = self.COMPOSED_TELEMETRY_PROSE
        # Isolation evidence: Gate A alone would accept; Gate B rejects.
        assert _count_cli_telemetry_keys(error) >= 2
        assert _is_raw_or_truncated_json_dump(error) is False

        result = _make_agent_result(ResultStatus.ERROR, error=error)
        assert _is_unparseable_cli_payload(result) is False
        assert _classify_attempt("fable", result, allow_retry=True) == ACTION_GIVE_UP

        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        mock_adapter.run.return_value = result

        out = run_with_fallback("test", {}, chain="fable:opus")
        assert out is None
        assert mock_adapter.run.call_count == 1


class TestEndToEndAgainstRealStderrTail:
    """Strongest evidence: real producer → real classifier → real chain advance.

    Hand-copied fragments can drift. This builds a realistic claude CLI envelope
    (subtype error_during_execution, is_error True, no message/result/error/detail
    key, full usage block) and feeds it through the REAL ``stderr_tail`` so the
    dump-and-truncate branch produces the fragment under test.
    """

    def _error_during_execution_envelope(self) -> dict:
        """Claude CLI envelope that forces the json.dumps(... )[-500:] branch."""
        return {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "duration_ms": 12345,
            "duration_api_ms": 11000,
            "num_turns": 3,
            "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "total_cost_usd": 0.123456,
            "permission_denials": [],
            "modelUsage": {
                "claude-opus-4": {
                    "inputTokens": 1000,
                    "outputTokens": 500,
                    "cacheReadInputTokens": 70850,
                    "cacheCreationInputTokens": 200,
                    "webSearchRequests": 0,
                    "costUSD": 0.12,
                    "contextWindow": 200000,
                    # Pad so the sorted dump exceeds 500 chars and truncates mid-key.
                    "notes": "x" * 200,
                }
            },
            "usage": {
                "cache_creation": {
                    "ephemeral_1h_input_tokens": 10,
                    "ephemeral_5m_input_tokens": 20,
                },
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 70850,
                "inference_geo": "not_available",
                "input_tokens": 6,
                "iterations": [{"type": "tool", "id": "iter-" + ("x" * 50)}],
                "output_tokens": 1262,
                "server_tool_use": {
                    "web_fetch_requests": 0,
                    "web_search_requests": 0,
                },
                "service_tier": "standard",
            },
            "uuid": "f1d5a1b2-c3d4-e5f6-a7b8-c9d0e1f2a3b4",
        }

    @patch("omniagentos.adapters.registry.resolve_adapter")
    def test_real_stderr_tail_truncated_dump_advances_chain(
        self, mock_resolve: MagicMock
    ) -> None:
        envelope = self._error_during_execution_envelope()
        # No result/message/error/detail → dump-and-truncate branch.
        assert not any(k in envelope for k in ("result", "message", "error", "detail"))
        raw = json.dumps(envelope)
        produced = stderr_tail(raw)

        assert len(produced) == 500
        # Blind tail slice cannot start a valid JSON document.
        assert not produced.startswith("{"), (
            f"expected mid-key truncation, got start={produced[:40]!r}"
        )

        result = _make_agent_result(ResultStatus.ERROR, error=produced)
        assert _is_unparseable_cli_payload(result) is True
        assert _classify_attempt("opus", result, allow_retry=True) == ACTION_ADVANCE

        mock_adapter = MagicMock()
        mock_resolve.return_value = mock_adapter
        opus_plan = {"plan": "from-opus-after-real-tail", "tasks": []}
        fable_err = _make_agent_result(ResultStatus.ERROR, error=produced)
        opus_ok = _make_agent_result(ResultStatus.OK, output_json=opus_plan)
        mock_adapter.run.side_effect = [fable_err, opus_ok]

        out = run_with_fallback("test", {}, chain="fable:opus")
        assert out == opus_plan
        assert mock_adapter.run.call_count == 2
