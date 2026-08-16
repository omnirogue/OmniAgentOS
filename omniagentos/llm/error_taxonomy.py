"""Error classification taxonomy for LLM provider responses.

This module defines an enumerated taxonomy of non-retryable LLM errors and provides
a classify() function that maps LLMError exceptions into semantic retry classes.
Unknown errors default to RETRYABLE (safe direction) with a log.
"""

from __future__ import annotations

import logging
import re
from enum import Enum

from omniagentos.llm.budget import LLMClientError, LLMError

_log = logging.getLogger(__name__)


class RetryClass(Enum):
    """Semantic classification of an LLM error's retry eligibility."""

    RETRYABLE = "retryable"
    """Transient failure: safe to retry with backoff (5xx, 429, network, timeouts)."""

    NON_RETRYABLE = "non_retryable"
    """Permanent failure: fail fast without retry (auth, bad request, quota exhaustion)."""


class NonRetryableClass(Enum):
    """Enumerated classes of non-retryable LLM errors."""

    AUTH_INVALID = "auth_invalid"
    """401/403: Invalid, expired, or rejected credentials or API key."""

    AUTH_PERMISSION = "auth_permission"
    """403: Valid credentials but insufficient permission for this operation."""

    BAD_REQUEST = "bad_request"
    """400/422: Invalid request schema, malformed JSON, or semantic violation."""

    GONE = "gone"
    """410: Resource no longer exists, operation obsolete."""

    PAYLOAD_TOO_LARGE = "payload_too_large"
    """413: Request body exceeds provider's limit."""

    UNSUPPORTED_MEDIA = "unsupported_media"
    """415: Content-Type or request format not supported by provider."""

    QUOTA_EXHAUSTED = "quota_exhausted"
    """Semantic quota exhaustion: hard limits, token limits, or model capacity."""

    MODEL_NOT_AVAILABLE = "model_not_available"
    """Model does not exist, is not deployed, or is unavailable in this region."""

    INVALID_RESPONSE = "invalid_response"
    """200 response whose body could not be parsed or is semantically invalid."""


def _extract_http_status(error: BaseException) -> int | None:
    """Extract HTTP status code from an exception chain, if present.

    Args:
        error: The exception, possibly wrapping urllib.error.HTTPError.

    Returns:
        HTTP status code if found, else None.
    """
    if hasattr(error, "code"):
        # urllib.error.HTTPError has .code attribute
        return error.code
    if hasattr(error, "__cause__") and error.__cause__:
        return _extract_http_status(error.__cause__)
    return None


def _extract_error_body(error: BaseException) -> str | None:
    """Extract error response body from an exception chain, if present.

    Args:
        error: The exception, possibly wrapping urllib.error.HTTPError.

    Returns:
        Error body text if found, else None.
    """
    if hasattr(error, "fp"):
        # urllib.error.HTTPError may have a file-like object fp
        try:
            body = error.fp.read()
            if isinstance(body, bytes):
                return body.decode("utf-8", errors="replace")
            return str(body)
        except Exception:
            pass
    if hasattr(error, "__cause__") and error.__cause__:
        return _extract_error_body(error.__cause__)
    return None


def _check_quota_phrase(text: str) -> bool:
    """Check if text contains quota exhaustion phrases (word-based matching)."""
    text_lower = text.lower()
    # Use regex to check for quota-related patterns (not strict substring matching)
    quota_patterns = [
        r"\bquota\s+(exceeded|exhausted)",
        r"\bcapacity\s+exhausted",
        r"\bresource\s+(exhausted|has\s+been\s+exhausted)",
        r"monthly\s+limit",
        r"daily\s+limit",
        r"token\s+limit",
        r"resource\s+exhausted",
        r"quota\s+exhausted",
    ]
    return bool(re.search("|".join(quota_patterns), text_lower))


def _check_model_unavailable_phrase(text: str) -> bool:
    """Check if text contains model unavailability phrases."""
    text_lower = text.lower()
    # Check for patterns like "model X is not available", "model not found", etc.
    return bool(
        re.search(
            r"\b(model.*?(not\s+available|not\s+found|does\s+not\s+exist|unknown|offline|disabled|unavailable))",
            text_lower,
        )
    ) or any(
        phrase in text_lower
        for phrase in [
            "model not available",
            "model not found",
            "unknown model",
            "model does not exist",
            "model is offline",
            "model is disabled",
        ]
    )


def _classify_from_status_and_message(
    status: int | None, message: str, body: str | None = None
) -> tuple[RetryClass, NonRetryableClass | None]:
    """Classify based on HTTP status and message text.

    This function first checks for semantic patterns in the message/body that
    indicate permanent errors (quota, model not available, etc.), then falls
    back to status code classification. Does not apply conservative defaults —
    that is the caller's responsibility.

    Args:
        status: HTTP status code, or None if not available.
        message: Error message string.
        body: Optional full response body.

    Returns:
        A tuple of (RetryClass, NonRetryableClass | None).
    """
    msg_lower = message.lower()
    body_lower = (body or "").lower()
    full_text = f"{msg_lower} {body_lower}"

    # Check for semantic patterns that indicate permanent errors
    # (these can occur in various HTTP status codes)

    # Quota exhaustion patterns (high priority)
    if _check_quota_phrase(full_text):
        return (RetryClass.NON_RETRYABLE, NonRetryableClass.QUOTA_EXHAUSTED)

    # Model not available patterns
    if _check_model_unavailable_phrase(full_text):
        return (RetryClass.NON_RETRYABLE, NonRetryableClass.MODEL_NOT_AVAILABLE)

    # Now classify by HTTP status (fallback if no semantic match)
    if status is None:
        # No status code and no semantic match: default to retryable
        return (RetryClass.RETRYABLE, None)

    # 401: Unauthorized
    if status == 401:
        return (RetryClass.NON_RETRYABLE, NonRetryableClass.AUTH_INVALID)

    # 403: Forbidden
    if status == 403:
        return (RetryClass.NON_RETRYABLE, NonRetryableClass.AUTH_PERMISSION)

    # 400: Bad Request
    if status == 400:
        return (RetryClass.NON_RETRYABLE, NonRetryableClass.BAD_REQUEST)

    # 410: Gone
    if status == 410:
        return (RetryClass.NON_RETRYABLE, NonRetryableClass.GONE)

    # 413: Payload Too Large
    if status == 413:
        return (RetryClass.NON_RETRYABLE, NonRetryableClass.PAYLOAD_TOO_LARGE)

    # 415: Unsupported Media Type
    if status == 415:
        return (RetryClass.NON_RETRYABLE, NonRetryableClass.UNSUPPORTED_MEDIA)

    # 422: Unprocessable Entity
    if status == 422:
        return (RetryClass.NON_RETRYABLE, NonRetryableClass.BAD_REQUEST)

    # 404: Not Found (could be model not found)
    if status == 404:
        if any(
            phrase in full_text
            for phrase in ["model", "not found", "does not exist", "unknown model"]
        ):
            return (RetryClass.NON_RETRYABLE, NonRetryableClass.MODEL_NOT_AVAILABLE)
        return (RetryClass.NON_RETRYABLE, NonRetryableClass.BAD_REQUEST)

    # 429: Rate limit (transient by default)
    if status == 429:
        return (RetryClass.RETRYABLE, None)

    # 5xx: Server errors (transient)
    if 500 <= status < 600:
        return (RetryClass.RETRYABLE, None)

    # For other 4xx: don't classify here, let caller apply conservative default
    if 400 <= status < 500:
        return (RetryClass.RETRYABLE, None)

    # Default: retryable
    return (RetryClass.RETRYABLE, None)


def classify(error: Exception) -> tuple[RetryClass, NonRetryableClass | None]:
    """Classify an LLM exception into a retry class.

    This function examines an LLMError and its exception chain to determine
    whether the error is retryable or permanently failed. Unknown patterns or
    parsing failures default to RETRYABLE (safe direction) with a log.

    Args:
        error: An LLMError (or subclass) raised by client.complete().

    Returns:
        A tuple of (RetryClass, NonRetryableClass | None). If retryable, the
        second element is None. If non-retryable, the second element names the
        specific non-retryable class.
    """
    if not isinstance(error, LLMError):
        _log.warning(
            "classify() received non-LLMError exception (defaulting to retryable): %s",
            type(error).__name__,
        )
        return (RetryClass.RETRYABLE, None)

    # LLMInvalidResponseError is always non-retryable: the provider answered
    # with a 200 but the response was unparseable. Retrying will not fix this.
    if error.__class__.__name__ == "LLMInvalidResponseError":
        return (RetryClass.NON_RETRYABLE, NonRetryableClass.INVALID_RESPONSE)

    message = str(error)
    status = _extract_http_status(error)
    body = _extract_error_body(error)

    # Classify based on message and status
    retry_class, non_retryable_class = _classify_from_status_and_message(status, message, body)

    # Conservative default: if it's an LLMClientError with a 4xx status and we
    # classified it as retryable, convert to non-retryable (because 4xx errors
    # are typically permanent). Log this so gaps in pattern matching are visible.
    if (
        retry_class == RetryClass.RETRYABLE
        and isinstance(error, LLMClientError)
        and status is not None
        and 400 <= status < 500
    ):
        _log.info(
            "classify() matched LLMClientError with 4xx status and no explicit semantic "
            "non-retryable pattern; defaulting to non-retryable (conservative): %s",
            message,
        )
        return (RetryClass.NON_RETRYABLE, NonRetryableClass.BAD_REQUEST)

    return (retry_class, non_retryable_class)
