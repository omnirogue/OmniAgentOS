"""jira_retry policy tests (JG1-E3, E4) — far side is request count / sleep value."""

from __future__ import annotations

from typing import Any

import httpx

from omniagentos.connectors.jira_client import JiraClient
from omniagentos.connectors.jira_retry import (
    MAX_RETRIES,
    classify_rate_limit_reason,
    with_retries,
)
from omniagentos.connectors.jira_retry import _is_blind_retry_safe as is_blind_retry_safe


class RecordingTransport(httpx.BaseTransport):
    def __init__(self, handler: Any) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def test_non_retryable_transition_post_is_not_blindly_retried() -> None:
    """JG1-E3: after 429 on transition POST, exactly one POST was sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/transitions"):
            return httpx.Response(429, headers={"Retry-After": "1"}, json={"error": "rate"})
        return httpx.Response(500, json={})

    transport = RecordingTransport(handler)
    sleeps: list[float] = []
    http = httpx.Client(transport=transport)
    client = JiraClient(
        base_url="https://example-team.atlassian.net",
        email="bot@example.com",
        api_token="tok",
        client=http,
        sleep=sleeps.append,
    )
    # Drive a transition POST through the retry-aware _request path.
    # list_transitions is GET; use raw _request for the transition POST shape.
    try:
        client._request(  # noqa: SLF001 — intentional: exercises retry on POST path
            "POST",
            "/rest/api/3/issue/ACM-1/transitions",
            json_body={"transition": {"id": "31"}},
        )
    except Exception:  # noqa: BLE001 — JiraError expected after single attempt
        pass
    posts = [r for r in transport.requests if r.method == "POST"]
    assert len(posts) == 1
    assert sleeps == []  # no retry → no sleep
    assert is_blind_retry_safe("POST", "/rest/api/3/issue/ACM-1/transitions") is False
    client.close()


def test_non_retryable_create_and_comment_paths() -> None:
    assert is_blind_retry_safe("POST", "/rest/api/3/issue") is False
    assert is_blind_retry_safe("POST", "/rest/api/3/issue/") is False
    assert is_blind_retry_safe("POST", "/rest/api/3/issue/ACM-1/comment") is False
    assert is_blind_retry_safe("POST", "/rest/api/3/search/jql") is True  # policy still classifies
    assert is_blind_retry_safe("GET", "/rest/api/3/search/jql") is True
    assert is_blind_retry_safe("GET", "/rest/api/3/myself") is True


def test_retry_after_honored_on_safe_get() -> None:
    """JG1-E4: Retry-After: 2 → recorded sleep ≥ 2.0; GET attempts ≤ 5 total."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "2"}, text="slow down")
        return httpx.Response(
            200,
            json={"accountId": "acc-1", "displayName": "Bot"},
        )

    transport = RecordingTransport(handler)
    sleeps: list[float] = []
    http = httpx.Client(transport=transport)
    client = JiraClient(
        base_url="https://example-team.atlassian.net",
        email="bot@example.com",
        api_token="tok",
        client=http,
        sleep=sleeps.append,
    )
    me = client.myself()
    assert me.account_id == "acc-1"
    assert attempts["n"] <= 5
    assert attempts["n"] == 3
    assert len(sleeps) == 2
    assert all(s >= 2.0 for s in sleeps)
    assert len(transport.requests) <= 5
    client.close()


def test_retry_after_caps_at_max_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "1"}, text="nope")

    transport = RecordingTransport(handler)
    sleeps: list[float] = []
    http = httpx.Client(transport=transport)
    client = JiraClient(
        base_url="https://example-team.atlassian.net",
        email="bot@example.com",
        api_token="tok",
        client=http,
        sleep=sleeps.append,
    )
    try:
        client.myself()
    except Exception:  # noqa: BLE001
        pass
    # 1 initial + MAX_RETRIES retries
    assert len(transport.requests) == MAX_RETRIES + 1
    assert len(sleeps) == MAX_RETRIES
    client.close()


def test_search_jql_get_is_retryable_on_429() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, text="wait")
        return httpx.Response(
            200,
            json={"issues": [{"id": "1", "key": "HOO-1", "fields": {}}], "isLast": True},
        )

    transport = RecordingTransport(handler)
    sleeps: list[float] = []
    http = httpx.Client(transport=transport)
    client = JiraClient(
        base_url="https://example-team.atlassian.net",
        email="bot@example.com",
        api_token="tok",
        client=http,
        sleep=sleeps.append,
    )
    issues = client.search_jql("project = HOO")
    assert len(issues) == 1
    assert len(sleeps) == 1
    assert sleeps[0] >= 2.0
    assert transport.requests[0].method == "GET"
    assert transport.requests[0].url.params.get("jql") == "project = HOO"
    client.close()


def test_rate_limit_reason_burst_is_per_request_backoff() -> None:
    """RateLimit-Reason: jira-burst → per-request sleep ≥ Retry-After; no quota pause."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                429,
                headers={
                    "Retry-After": "2",
                    "RateLimit-Reason": "jira-burst-based",
                },
                text="burst",
            )
        return httpx.Response(200, json={"accountId": "a", "displayName": "Bot"})

    transport = RecordingTransport(handler)
    sleeps: list[float] = []
    http = httpx.Client(transport=transport)
    client = JiraClient(
        base_url="https://example-team.atlassian.net",
        email="bot@example.com",
        api_token="tok",
        client=http,
        sleep=sleeps.append,
    )
    client.myself()
    assert attempts["n"] == 2
    assert len(sleeps) == 1
    assert sleeps[0] >= 2.0
    assert client._quota_pause_until == 0.0  # noqa: SLF001
    assert classify_rate_limit_reason("jira-burst-based") == "burst"
    client.close()


def test_rate_limit_reason_quota_pauses_client_wide() -> None:
    """RateLimit-Reason: jira-quota-* → client-wide pause-until-reset."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                429,
                headers={
                    "Retry-After": "3",
                    "RateLimit-Reason": "jira-quota-tenant-based",
                    "X-RateLimit-Reset": "3",
                },
                text="quota",
            )
        return httpx.Response(200, json={"accountId": "a", "displayName": "Bot"})

    transport = RecordingTransport(handler)
    sleeps: list[float] = []
    http = httpx.Client(transport=transport)
    client = JiraClient(
        base_url="https://example-team.atlassian.net",
        email="bot@example.com",
        api_token="tok",
        client=http,
        sleep=sleeps.append,
    )
    client.myself()
    assert attempts["n"] == 2
    assert any(s >= 3.0 for s in sleeps)
    # Quota pause recorded so a subsequent call would wait without another 429.
    assert client._quota_pause_until > 0.0  # noqa: SLF001
    assert classify_rate_limit_reason("jira-quota-global-based") == "quota"
    client.close()


def test_rate_limit_reason_per_issue_backoffs_that_issue_only() -> None:
    """RateLimit-Reason: jira-per-issue-on-write → per-issue backoff map."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                429,
                headers={
                    "Retry-After": "2",
                    "RateLimit-Reason": "jira-per-issue-on-write",
                },
                text="per-issue",
            )
        return httpx.Response(
            200,
            json={"id": "1", "key": "ACM-1", "fields": {"summary": "x"}},
        )

    transport = RecordingTransport(handler)
    sleeps: list[float] = []
    http = httpx.Client(transport=transport)
    client = JiraClient(
        base_url="https://example-team.atlassian.net",
        email="bot@example.com",
        api_token="tok",
        client=http,
        sleep=sleeps.append,
    )
    client.get_issue("ACM-1")
    assert "ACM-1" in client._issue_backoff_until  # noqa: SLF001
    assert client._issue_backoff_until["ACM-1"] > 0.0  # noqa: SLF001
    assert client._quota_pause_until == 0.0  # noqa: SLF001 — not client-wide
    assert classify_rate_limit_reason("jira-per-issue-on-write") == "per_issue"
    # with_retries still used for the transport path
    assert with_retries is not None
    client.close()
