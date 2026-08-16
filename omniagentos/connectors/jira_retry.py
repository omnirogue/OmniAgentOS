"""Retry policy for Jira Cloud HTTP calls.

Safe GETs (and the read-shaped POST ``/search/jql``) may retry on 429/5xx with
``Retry-After`` honoured and exponential backoff × jitter, at most 4 retries
(5 total attempts). Transition / create / comment POSTs are never blindly
retried — the far side is request count, not a classifier return value.

``RateLimit-Reason`` classes (spec §12 / gate E4):
  * ``jira-burst*`` → per-request backoff (Retry-After floor + exp jitter)
  * ``jira-quota-*`` → client-wide pause-until-reset (caller records pause)
  * ``jira-per-issue*`` → per-issue backoff (caller records issue pause)
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

# Max retries after the first attempt → total attempts ≤ 5.
MAX_RETRIES = 4
BASE_BACKOFF_S = 0.5
JITTER_LO = 0.7
JITTER_HI = 1.3

RateLimitClass = Literal["burst", "quota", "per_issue", "unknown"]

_SEARCH_JQL_RE = re.compile(r"/rest/api/3/search/jql/?$")
_BULKFETCH_RE = re.compile(r"/rest/api/3/issue/bulkfetch/?$")
_TRANSITION_RE = re.compile(r"/rest/api/3/issue/[^/]+/transitions/?$")
_COMMENT_RE = re.compile(r"/rest/api/3/issue/[^/]+/comment/?$")
_CREATE_ISSUE_RE = re.compile(r"/rest/api/3/issue/?$")
_ISSUE_KEY_RE = re.compile(r"/rest/api/3/issue/([A-Za-z][A-Za-z0-9]+-\d+)")


def _path_of(url_or_path: str) -> str:
    if "://" in url_or_path:
        return urlparse(url_or_path).path or ""
    return url_or_path


def issue_key_from_path(url_or_path: str) -> str | None:
    """Extract ``PROJ-123`` from a Jira issue path, if present."""
    match = _ISSUE_KEY_RE.search(_path_of(url_or_path))
    return match.group(1) if match else None


def classify_rate_limit_reason(header_value: str | None) -> RateLimitClass:
    """Map ``RateLimit-Reason`` to a policy class (spec §12)."""
    if header_value is None:
        return "unknown"
    raw = str(header_value).strip().lower()
    if not raw:
        return "unknown"
    if "quota" in raw:
        return "quota"
    if "per-issue" in raw or "per_issue" in raw:
        return "per_issue"
    if "burst" in raw:
        return "burst"
    return "unknown"


def _is_blind_retry_safe(method: str, url_or_path: str) -> bool:
    """Whether a failed call may be auto-retried without re-checking intent.

    GET → yes. POST ``/search/jql`` and ``/issue/bulkfetch`` (read-shaped) → yes.
    POST transition / create / comment → never. Unknown mutating methods → no.
    """
    method_u = method.upper()
    path = _path_of(url_or_path)
    if method_u in {"GET", "HEAD", "OPTIONS"}:
        return True
    if method_u == "POST" and (_SEARCH_JQL_RE.search(path) or _BULKFETCH_RE.search(path)):
        return True
    if method_u == "POST" and (
        _TRANSITION_RE.search(path) or _COMMENT_RE.search(path) or _CREATE_ISSUE_RE.search(path)
    ):
        return False
    # PUT edit is "careful" per GROK-JIRA-API-SPEC — never blind-retry.
    return False


def _parse_retry_after(header_value: str | None) -> float | None:
    """Parse a ``Retry-After`` header (seconds only; HTTP-date ignored → None)."""
    if header_value is None:
        return None
    raw = str(header_value).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


def parse_rate_limit_reset(header_value: str | None, *, now: float | None = None) -> float | None:
    """Seconds until ``X-RateLimit-Reset`` (ISO-8601) or numeric epoch.

    Returns a non-negative delay in seconds, or None if unparseable.
    """
    if header_value is None:
        return None
    raw = str(header_value).strip()
    if not raw:
        return None
    # Numeric epoch seconds.
    try:
        epoch = float(raw)
        if epoch > 1e12:  # ms epoch
            epoch = epoch / 1000.0
        base = now if now is not None else datetime.now().timestamp()
        delay = epoch - base
        return max(0.0, delay) if delay == delay else None  # NaN guard
    except ValueError:
        pass
    # ISO-8601.
    try:
        cleaned = raw.replace("Z", "+00:00")
        reset_dt = datetime.fromisoformat(cleaned)
        base_ts = now if now is not None else datetime.now().timestamp()
        delay = reset_dt.timestamp() - base_ts
        return max(0.0, delay)
    except ValueError:
        return None


def _compute_sleep_seconds(
    *,
    attempt: int,
    retry_after: float | None,
    rng: random.Random | None = None,
) -> float:
    """Delay for this retry attempt (0-based attempt index after the first try).

    Exponential backoff is jittered; ``Retry-After`` is a hard floor so the
    recorded sleep is always ≥ the server-requested wait (JG1-E4).
    """
    exp = BASE_BACKOFF_S * (2**attempt)
    picker = rng.uniform if rng is not None else random.uniform
    jitter = picker(JITTER_LO, JITTER_HI)
    jittered = float(exp) * float(jitter)
    return max(float(retry_after or 0.0), jittered)


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """Outcome of the retry policy for one failed response (for tests / logging)."""

    retry: bool
    sleep_s: float
    reason: str
    rate_limit_class: RateLimitClass = "unknown"


def _decide_retry(
    *,
    method: str,
    url_or_path: str,
    status_code: int,
    attempt: int,
    retry_after_header: str | None,
    rate_limit_reason_header: str | None = None,
    rate_limit_reset_header: str | None = None,
    rng: random.Random | None = None,
    now: float | None = None,
) -> RetryDecision:
    """Decide whether to retry. ``attempt`` is the number of failures so far (1..)."""
    rl_class = classify_rate_limit_reason(rate_limit_reason_header)
    if attempt > MAX_RETRIES:
        return RetryDecision(False, 0.0, "max_retries", rl_class)
    if status_code not in {429, 500, 502, 503, 504}:
        return RetryDecision(False, 0.0, "status_not_retryable", rl_class)
    if not _is_blind_retry_safe(method, url_or_path):
        return RetryDecision(False, 0.0, "method_not_retryable", rl_class)

    retry_after = _parse_retry_after(retry_after_header)
    # Quota-wide: prefer X-RateLimit-Reset when present (pause-until-reset).
    if rl_class == "quota":
        reset_delay = parse_rate_limit_reset(rate_limit_reset_header, now=now)
        floor = max(float(retry_after or 0.0), float(reset_delay or 0.0))
        sleep_s = _compute_sleep_seconds(
            attempt=max(0, attempt - 1),
            retry_after=floor if floor > 0 else retry_after,
            rng=rng,
        )
        return RetryDecision(True, sleep_s, "quota_pause", rl_class)

    # Burst and per-issue (and unknown): per-request backoff with Retry-After floor.
    sleep_s = _compute_sleep_seconds(
        attempt=max(0, attempt - 1),
        retry_after=retry_after,
        rng=rng,
    )
    reason = "burst_backoff" if rl_class == "burst" else (
        "per_issue_backoff" if rl_class == "per_issue" else "retry"
    )
    return RetryDecision(True, sleep_s, reason, rl_class)


def with_retries(
    send: Callable[[], Any],
    *,
    method: str,
    url_or_path: str,
    sleep: Callable[[float], None],
    status_of: Callable[[Any], int],
    retry_after_of: Callable[[Any], str | None],
    is_error: Callable[[Any], bool],
    rate_limit_reason_of: Callable[[Any], str | None] | None = None,
    rate_limit_reset_of: Callable[[Any], str | None] | None = None,
    on_rate_limit: Callable[[RetryDecision, Any], None] | None = None,
    rng: random.Random | None = None,
    now: Callable[[], float] | None = None,
) -> Any:
    """Execute ``send`` with the Jira retry policy; return the last response/result.

    ``send`` must perform exactly one HTTP attempt per call. Sleeps are routed
    through ``sleep`` so tests can record the delay value.

    ``on_rate_limit`` is invoked on every 429 before the sleep so the client can
    record quota-wide / per-issue pause state even when the method is not
    blind-retry-safe (decision.retry may be False).
    """
    attempt = 0
    clock = now or (lambda: datetime.now().timestamp())
    while True:
        result = send()
        if not is_error(result):
            return result
        attempt += 1
        status = status_of(result)
        reason_h = rate_limit_reason_of(result) if rate_limit_reason_of else None
        reset_h = rate_limit_reset_of(result) if rate_limit_reset_of else None
        decision = _decide_retry(
            method=method,
            url_or_path=url_or_path,
            status_code=status,
            attempt=attempt,
            retry_after_header=retry_after_of(result),
            rate_limit_reason_header=reason_h,
            rate_limit_reset_header=reset_h,
            rng=rng,
            now=clock(),
        )
        if status == 429 and on_rate_limit is not None:
            on_rate_limit(decision, result)
        if not decision.retry:
            return result
        sleep(decision.sleep_s)


__all__ = [
    "MAX_RETRIES",
    "RetryDecision",
    "classify_rate_limit_reason",
    "issue_key_from_path",
    "parse_rate_limit_reset",
    "with_retries",
]
