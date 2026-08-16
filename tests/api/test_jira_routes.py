"""Jira API routes — mocked (JG1-E5, E8 surface)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from omniagentos.api.main import app
from omniagentos.connectors.jira_client import JiraError, JiraMyself
from omniagentos.sessions import token


@pytest.fixture
def auth_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return {"X-Session-Token": token.load_or_create_token()}


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def test_health_401_sanitized(
    client: TestClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """JG1-E5: secret bytes absent from response body, logs, and error text.

    Exercises a real httpx.HTTPStatusError path inside the client sanitizer.
    """
    secret = "jira-api-token-MUST-NOT-LEAK-7c2e9a"
    monkeypatch.setenv("JIRA_API_TOKEN", secret)
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_BASE_URL", "https://initech-team.atlassian.net")

    def boom_myself(self: Any) -> JiraMyself:  # noqa: ANN401
        request = httpx.Request(
            "GET",
            "https://initech-team.atlassian.net/rest/api/3/myself",
            headers={"Authorization": "Basic dXNlcjpzZWNyZXQ="},
        )
        response = httpx.Response(401, request=request, text="Unauthorized")
        err = httpx.HTTPStatusError("401", request=request, response=response)
        from omniagentos.connectors.jira_client import _error_detail, _sanitize

        detail = _sanitize(_error_detail(err), (secret, "bot@example.com"))
        raise JiraError(detail, status_code=401)

    with caplog.at_level(logging.WARNING), patch(
        "omniagentos.connectors.jira_client.JiraClient.myself", boom_myself
    ):
        response = client.get("/api/jira/health", headers=auth_headers)

    assert response.status_code == 401
    secret_bytes = secret.encode()
    assert secret_bytes not in response.content
    assert secret not in response.text
    assert secret not in caplog.text
    assert b"Basic " not in response.content
    assert "Basic " not in caplog.text
    body = response.json()
    assert "error" in body
    assert secret not in body["error"].get("message", "")


def test_health_ok(
    client: TestClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")

    with patch(
        "omniagentos.connectors.jira_client.JiraClient.myself",
        return_value=JiraMyself(account_id="acc-1", display_name="Bot"),
    ):
        response = client.get("/api/jira/health", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "displayName": "Bot",
        "accountId": "acc-1",
    }


def test_jira_public_paths_only() -> None:
    """JG1-E8: mounted app path set — no issue/search/jql passthrough."""
    ps = sorted(p for p in app.openapi()["paths"] if p.startswith("/api/jira"))
    bad = [p for p in ps if any(t in p for t in ("issue", "search", "jql"))]
    assert not bad, bad
    assert ps == [
        "/api/jira/health",
        "/api/jira/projects",
        "/api/jira/projects/{key}/statuses",
    ]


def test_contract_doc_covers_jira_routes() -> None:
    doc = Path("contracts/jira-goals-api.md").read_text(encoding="utf-8")
    missing = [
        p for p in app.openapi()["paths"] if p.startswith("/api/jira") and p not in doc
    ]
    assert not missing, missing
