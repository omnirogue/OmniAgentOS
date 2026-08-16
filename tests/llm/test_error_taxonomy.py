"""Unit tests for the LLM error taxonomy and classification."""

from __future__ import annotations

import logging
import urllib.error

from omniagentos.llm.budget import (
    LLMClientError,
    LLMError,
    LLMInvalidResponseError,
    LLMTransportError,
)
from omniagentos.llm.error_taxonomy import (
    NonRetryableClass,
    RetryClass,
    classify,
)


class TestRetryClassEnum:
    """Tests for the RetryClass enum."""

    def test_retry_class_values(self):
        """Verify RetryClass enum members have expected string values."""
        assert RetryClass.RETRYABLE.value == "retryable"
        assert RetryClass.NON_RETRYABLE.value == "non_retryable"


class TestNonRetryableClassEnum:
    """Tests for the NonRetryableClass enum."""

    def test_non_retryable_class_values(self):
        """Verify all NonRetryableClass members have expected string values."""
        expected_classes = {
            "AUTH_INVALID": "auth_invalid",
            "AUTH_PERMISSION": "auth_permission",
            "BAD_REQUEST": "bad_request",
            "GONE": "gone",
            "PAYLOAD_TOO_LARGE": "payload_too_large",
            "UNSUPPORTED_MEDIA": "unsupported_media",
            "QUOTA_EXHAUSTED": "quota_exhausted",
            "MODEL_NOT_AVAILABLE": "model_not_available",
            "INVALID_RESPONSE": "invalid_response",
        }
        for member_name, expected_value in expected_classes.items():
            assert getattr(NonRetryableClass, member_name).value == expected_value


class TestClassifyAuthErrors:
    """Tests for authentication-related non-retryable errors."""

    def test_classify_401_unauthorized(self):
        """Verify HTTP 401 (Unauthorized) is classified as AUTH_INVALID."""
        http_error = urllib.error.HTTPError("http://test", 401, "Unauthorized", {}, None)
        exc = LLMClientError("HTTP client error 401: Unauthorized")
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.AUTH_INVALID

    def test_classify_403_permission_denied(self):
        """Verify HTTP 403 without quota phrases is classified as AUTH_PERMISSION."""
        http_error = urllib.error.HTTPError("http://test", 403, "Forbidden", {}, None)
        exc = LLMClientError("HTTP client error 403: Forbidden")
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.AUTH_PERMISSION

    def test_classify_403_with_quota_phrase(self):
        """Verify HTTP 403 with quota phrase is classified as QUOTA_EXHAUSTED."""
        http_error = urllib.error.HTTPError(
            "http://test", 403, "Quota exceeded for this API", {}, None
        )
        exc = LLMClientError("HTTP client error 403: Quota exceeded for this API")
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.QUOTA_EXHAUSTED


class TestClassifyBadRequestErrors:
    """Tests for bad request / schema violation errors."""

    def test_classify_400_bad_request(self):
        """Verify HTTP 400 is classified as BAD_REQUEST."""
        http_error = urllib.error.HTTPError(
            "http://test", 400, "Bad Request: invalid JSON", {}, None
        )
        exc = LLMClientError("HTTP client error 400: Bad Request")
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.BAD_REQUEST

    def test_classify_422_unprocessable_entity(self):
        """Verify HTTP 422 is classified as BAD_REQUEST."""
        http_error = urllib.error.HTTPError(
            "http://test",
            422,
            "Unprocessable Entity: missing required field",
            {},
            None,
        )
        exc = LLMClientError("HTTP client error 422: Unprocessable Entity")
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.BAD_REQUEST


class TestClassifyResourceErrors:
    """Tests for resource-related errors."""

    def test_classify_410_gone(self):
        """Verify HTTP 410 (Gone) is classified as GONE."""
        http_error = urllib.error.HTTPError("http://test", 410, "Gone", {}, None)
        exc = LLMClientError("HTTP client error 410: Gone")
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.GONE

    def test_classify_413_payload_too_large(self):
        """Verify HTTP 413 is classified as PAYLOAD_TOO_LARGE."""
        http_error = urllib.error.HTTPError(
            "http://test", 413, "Payload Too Large", {}, None
        )
        exc = LLMClientError("HTTP client error 413: Payload Too Large")
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.PAYLOAD_TOO_LARGE

    def test_classify_415_unsupported_media_type(self):
        """Verify HTTP 415 is classified as UNSUPPORTED_MEDIA."""
        http_error = urllib.error.HTTPError(
            "http://test", 415, "Unsupported Media Type", {}, None
        )
        exc = LLMClientError("HTTP client error 415: Unsupported Media Type")
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.UNSUPPORTED_MEDIA


class TestClassifyQuotaErrors:
    """Tests for quota exhaustion errors."""

    def test_classify_quota_exhausted_in_message(self):
        """Verify 'quota exceeded' in message is classified as QUOTA_EXHAUSTED."""
        http_error = urllib.error.HTTPError(
            "http://test", 429, "Quota exceeded for this operation", {}, None
        )
        exc = LLMTransportError("Quota exceeded for this operation")
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.QUOTA_EXHAUSTED

    def test_classify_monthly_limit_exhausted(self):
        """Verify 'monthly limit' phrase is classified as QUOTA_EXHAUSTED."""
        http_error = urllib.error.HTTPError(
            "http://test", 403, "Monthly limit exhausted", {}, None
        )
        exc = LLMClientError("Monthly limit exhausted for your API tier")
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.QUOTA_EXHAUSTED

    def test_classify_daily_limit_exhausted(self):
        """Verify 'daily limit' phrase is classified as QUOTA_EXHAUSTED."""
        exc = LLMClientError("Daily limit of 1000 calls exhausted")

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.QUOTA_EXHAUSTED


class TestClassifyModelNotAvailableErrors:
    """Tests for model availability errors."""

    def test_classify_404_model_not_found(self):
        """Verify HTTP 404 with 'model' phrase is classified as MODEL_NOT_AVAILABLE."""
        http_error = urllib.error.HTTPError("http://test", 404, "Not Found", {}, None)
        exc = LLMClientError("HTTP client error 404: Model not found")
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.MODEL_NOT_AVAILABLE

    def test_classify_model_not_available_phrase(self):
        """Verify 'model not available' phrase is classified as MODEL_NOT_AVAILABLE."""
        exc = LLMClientError("Model gpt-9999-super is not available in your region")

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.MODEL_NOT_AVAILABLE

    def test_classify_unknown_model(self):
        """Verify 'unknown model' phrase is classified as MODEL_NOT_AVAILABLE."""
        exc = LLMClientError("Unknown model: futuristic-model-v1")

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.MODEL_NOT_AVAILABLE

    def test_classify_model_is_offline(self):
        """Verify 'model is offline' phrase is classified as MODEL_NOT_AVAILABLE."""
        exc = LLMClientError("The requested model is offline for maintenance")

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.MODEL_NOT_AVAILABLE


class TestClassifyInvalidResponseErrors:
    """Tests for invalid response errors."""

    def test_classify_invalid_response_error(self):
        """Verify LLMInvalidResponseError is classified as INVALID_RESPONSE."""
        exc = LLMInvalidResponseError("Response body is not valid JSON")

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.INVALID_RESPONSE

    def test_classify_malformed_api_response(self):
        """Verify malformed API response is classified as INVALID_RESPONSE."""
        exc = LLMInvalidResponseError("Malformed API response structure: missing choices")

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.INVALID_RESPONSE


class TestClassifyTransportErrors:
    """Tests for transient transport errors."""

    def test_classify_transport_error_retryable(self):
        """Verify LLMTransportError defaults to RETRYABLE."""
        exc = LLMTransportError("Network timeout: connection reset")

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.RETRYABLE
        assert non_retryable is None

    def test_classify_503_service_unavailable(self):
        """Verify HTTP 503 (transient) is classified as RETRYABLE."""
        http_error = urllib.error.HTTPError(
            "http://test", 503, "Service Unavailable", {}, None
        )
        exc = LLMTransportError("HTTP transient error 503: Service Unavailable")
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.RETRYABLE
        assert non_retryable is None

    def test_classify_500_internal_server_error(self):
        """Verify HTTP 500 (transient) is classified as RETRYABLE."""
        http_error = urllib.error.HTTPError(
            "http://test", 500, "Internal Server Error", {}, None
        )
        exc = LLMTransportError("HTTP transient error 500: Internal Server Error")
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.RETRYABLE
        assert non_retryable is None

    def test_classify_429_rate_limit_transient(self):
        """Verify HTTP 429 (rate limit, transient) is classified as RETRYABLE by default."""
        http_error = urllib.error.HTTPError(
            "http://test", 429, "Too Many Requests", {}, None
        )
        exc = LLMTransportError("HTTP transient error 429: Too Many Requests")
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.RETRYABLE
        assert non_retryable is None


class TestClassifyFallbackBehavior:
    """Tests for fallback classification behavior."""

    def test_classify_unknown_llm_error_subclass(self):
        """Verify unknown LLMError subclass defaults to RETRYABLE."""
        # Create a custom LLMError subclass
        custom_error = LLMError("Unknown error type")

        retry_class, non_retryable = classify(custom_error)
        assert retry_class == RetryClass.RETRYABLE
        assert non_retryable is None

    def test_classify_non_llm_error_defaults_retryable(self):
        """Verify non-LLMError exceptions default to RETRYABLE with warning."""
        exc = ValueError("Not an LLMError")

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.RETRYABLE
        assert non_retryable is None

    def test_classify_logs_unknown_types(self, caplog):
        """Verify unknown error types are logged."""
        exc = ValueError("Some other error")

        with caplog.at_level(logging.WARNING):
            classify(exc)

        assert "non-LLMError" in caplog.text

    def test_classify_logs_unmatched_client_errors(self, caplog):
        """Verify unmatched LLMClientError is logged as conservative default."""
        # Create a client error with an HTTP status that doesn't match our patterns
        http_error = urllib.error.HTTPError("http://test", 418, "I'm a teapot", {}, None)
        exc = LLMClientError("HTTP client error 418: I'm a teapot")
        exc.__cause__ = http_error

        with caplog.at_level(logging.INFO):
            retry_class, non_retryable = classify(exc)

        assert "no explicit" in caplog.text and "4xx status" in caplog.text
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.BAD_REQUEST


class TestClassifyRealWorldScenarios:
    """Integration tests with realistic error payloads."""

    def test_classify_anthropic_auth_error(self):
        """Verify Anthropic-style 401 auth error."""
        exc = LLMClientError(
            "HTTP client error 401: Invalid authentication token. "
            "Please check your API key."
        )
        http_error = urllib.error.HTTPError(
            "https://api.anthropic.com/v1/messages", 401, "Unauthorized", {}, None
        )
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.AUTH_INVALID

    def test_classify_gemini_quota_error(self):
        """Verify Google Gemini quota exhaustion error."""
        exc = LLMClientError(
            "HTTP client error 429: {"
            '"error": {"code": 429, "message": "Resource has been exhausted (e.g. quota, '
            'rate limit).", "status": "RESOURCE_EXHAUSTED"}}'
        )
        http_error = urllib.error.HTTPError(
            "https://generativelanguage.googleapis.com/v1", 429, "Too Many Requests", {}, None
        )
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        # 429 with "exhausted" phrase should match quota exhaustion
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.QUOTA_EXHAUSTED

    def test_classify_openai_rate_limit_error(self):
        """Verify OpenAI-style rate limit (transient 429)."""
        exc = LLMTransportError(
            "HTTP transient error 429: Rate limit exceeded. "
            "Please retry after 60 seconds."
        )
        http_error = urllib.error.HTTPError(
            "https://api.openai.com/v1/chat/completions",
            429,
            "Too Many Requests",
            {},
            None,
        )
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        # Plain 429 without "exhausted" is transient
        assert retry_class == RetryClass.RETRYABLE
        assert non_retryable is None

    def test_classify_model_deprecated_error(self):
        """Verify OpenAI-style model deprecation (410 Gone)."""
        exc = LLMClientError(
            "HTTP client error 410: The model 'gpt-3.5-turbo' is deprecated. "
            "Please upgrade to 'gpt-4o-mini'."
        )
        http_error = urllib.error.HTTPError(
            "https://api.openai.com/v1/chat/completions", 410, "Gone", {}, None
        )
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.GONE

    def test_classify_malformed_json_response(self):
        """Verify unparseable 200 response (invalid response)."""
        exc = LLMInvalidResponseError(
            "Invalid response format: body is not valid JSON: "
            "Expecting value: line 1 column 1 (char 0)"
        )

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.INVALID_RESPONSE

    def test_classify_missing_response_keys(self):
        """Verify missing required keys in otherwise valid JSON."""
        exc = LLMInvalidResponseError(
            "JSON response missing required keys: ['choices']. "
            'Parsed object: {"data": []}'
        )

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable == NonRetryableClass.INVALID_RESPONSE

    def test_classify_network_timeout(self):
        """Verify network timeout is retryable."""
        exc = LLMTransportError("Connection transport error: timed out")

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.RETRYABLE
        assert non_retryable is None

    def test_classify_dns_resolution_error(self):
        """Verify DNS resolution error is retryable."""
        exc = LLMTransportError(
            "Network transport error: [Errno -2] Name or service not known"
        )

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.RETRYABLE
        assert non_retryable is None


class TestClassifyDocstringExamples:
    """Tests based on docstring examples."""

    def test_classify_returns_tuple_format(self):
        """Verify classify() returns a properly formatted tuple."""
        exc = LLMTransportError("Test error")
        result = classify(exc)

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], RetryClass)
        assert result[1] is None or isinstance(result[1], NonRetryableClass)

    def test_classify_non_retryable_has_secondary_class(self):
        """Verify non-retryable results include specific class."""
        exc = LLMClientError("HTTP client error 401: Unauthorized")
        http_error = urllib.error.HTTPError("http://test", 401, "Unauthorized", {}, None)
        exc.__cause__ = http_error

        retry_class, non_retryable = classify(exc)
        assert retry_class == RetryClass.NON_RETRYABLE
        assert non_retryable is not None
        assert isinstance(non_retryable, NonRetryableClass)
