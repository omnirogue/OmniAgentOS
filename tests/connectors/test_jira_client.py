"""JiraClient mocked transport tests (JG1-E1, E2) — zero network."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omniagentos.connectors.jira_client import JiraClient, JiraError


class RecordingTransport(httpx.BaseTransport):
    """Records requests; returns scripted responses by path+method."""

    def __init__(self, handler: Any) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def _client(transport: RecordingTransport, **kwargs: Any) -> JiraClient:
    http = httpx.Client(
        transport=transport,
        base_url="https://example-team.atlassian.net",
    )
    return JiraClient(
        base_url="https://example-team.atlassian.net",
        email="bot@example.com",
        api_token="test-token-not-real",
        client=http,
        sleep=lambda _s: None,
        **kwargs,
    )


def test_search_jql_uses_search_jql_path_only() -> None:
    """JG1-E1: transport SAW GET /rest/api/3/search/jql; never legacy /search."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.params.get("jql")
        assert request.url.params.get("fields")
        assert request.url.params.get("maxResults") is not None
        return httpx.Response(
            200,
            json={
                "issues": [
                    {
                        "id": "1",
                        "key": "ACM-1",
                        "fields": {"summary": "x", "status": {"name": "In Progress"}},
                    }
                ],
                "isLast": True,
            },
        )

    transport = RecordingTransport(handler)
    with _client(transport) as client:
        issues = client.search_jql('project = ACM AND statusCategory != Done')
    assert len(issues) == 1
    assert issues[0].key == "ACM-1"
    assert [r.url.path for r in transport.requests] == ["/rest/api/3/search/jql"]
    assert all(r.method == "GET" for r in transport.requests)
    assert not [r for r in transport.requests if r.url.path == "/rest/api/3/search"]


def test_page_without_next_token_stops_without_invented_total() -> None:
    """JG1-E2: page missing nextPageToken/isLast must not invent total pagination.

    Far side: request count == 1 (no second page, no unbounded loop).
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # Deliberately omit nextPageToken and isLast; include a decoy total that
        # a broken client might use for startAt loops.
        return httpx.Response(
            200,
            json={
                "issues": [
                    {"id": "1", "key": "INI-1", "fields": {"summary": "a"}},
                    {"id": "2", "key": "INI-2", "fields": {"summary": "b"}},
                ],
                "total": 200,
            },
        )

    transport = RecordingTransport(handler)
    with _client(transport) as client:
        issues = client.search_jql("project = INI")
    assert len(issues) == 2
    assert len(transport.requests) == 1
    assert calls["n"] == 1
    assert all(r.url.path == "/rest/api/3/search/jql" for r in transport.requests)


def test_search_jql_paginates_with_next_page_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.params.get("nextPageToken") is None:
            return httpx.Response(
                200,
                json={
                    "issues": [{"id": "1", "key": "CA-1", "fields": {}}],
                    "nextPageToken": "tok-2",
                    "isLast": False,
                },
            )
        return httpx.Response(
            200,
            json={
                "issues": [{"id": "2", "key": "CA-2", "fields": {}}],
                "isLast": True,
            },
        )

    transport = RecordingTransport(handler)
    with _client(transport) as client:
        issues = client.search_jql("project = CA")
    assert [i.key for i in issues] == ["CA-1", "CA-2"]
    assert len(transport.requests) == 2
    assert transport.requests[1].url.params.get("nextPageToken") == "tok-2"


def test_http_status_error_is_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errorMessages": ["auth"]})

    transport = RecordingTransport(handler)
    with _client(transport) as client:
        with pytest.raises(JiraError) as ei:
            client.myself()
    msg = str(ei.value)
    assert "test-token-not-real" not in msg
    assert "Basic " not in msg
    assert "401" in msg
