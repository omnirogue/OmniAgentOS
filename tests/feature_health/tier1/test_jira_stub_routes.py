"""Feature-health tier1 — Jira routes against a LOCAL loopback stub (no network).

Stands up a deterministic loopback-only HTTP stub (same idiom as
``tests/providers/contract_server.py``) that serves the three Jira Cloud REST v3
read endpoints the product client uses, then drives the REAL API routes through
FastAPI TestClient (idioms from ``tests/api/test_jira_routes.py``):

* ``/api/jira/projects`` paginates fully across two stub pages (isLast/startAt);
* ``/api/jira/projects/{key}/statuses`` dedups statuses by name across
  issue-type groups;
* ``/api/jira/health`` 200 shape, and 503 ``jira_unconfigured`` when the env
  credentials are absent;
* client-level rate-limit pause enforcement: a 429 with
  ``RateLimit-Reason: jira-per-issue-on-write`` makes the NEXT send to that
  issue sleep first (``tests/connectors/test_jira_retry.py`` idioms);
* guard: no public write surface — no ``put_issue_fields`` attribute on
  :class:`JiraClient` and no non-GET ``/api/jira`` method in the OpenAPI paths.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from omniagentos.api.main import app
from omniagentos.connectors.jira_client import JiraClient
from omniagentos.sessions import token

_PAGE_ONE = [
    {"id": "10001", "key": "ALPHA", "name": "Alpha", "projectTypeKey": "software"},
    {"id": "10002", "key": "BRAVO", "name": "Bravo", "projectTypeKey": "software"},
]
_PAGE_TWO = [
    {"id": "10003", "key": "CHARLIE", "name": "Charlie", "projectTypeKey": "business"},
]

# Grouped by issue type with deliberate duplicate status names across groups.
_STATUS_GROUPS = [
    {
        "id": "1",
        "name": "Task",
        "statuses": [
            {"id": "10", "name": "To Do", "statusCategory": {"key": "new"}},
            {"id": "11", "name": "In Progress", "statusCategory": {"key": "indeterminate"}},
            {"id": "12", "name": "Done", "statusCategory": {"key": "done"}},
        ],
    },
    {
        "id": "2",
        "name": "Bug",
        "statuses": [
            # Same names, different ids — the route must dedup by NAME.
            {"id": "20", "name": "To Do", "statusCategory": {"key": "new"}},
            {"id": "22", "name": "Done", "statusCategory": {"key": "done"}},
            {"id": "23", "name": "Blocked", "statusCategory": {"key": "indeterminate"}},
        ],
    },
]


class _JiraStubState:
    """Thread-safe request log (contract_server.py idiom)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def record(self, entry: dict[str, Any]) -> None:
        with self.lock:
            self.requests.append(entry)

    def search_pages(self) -> list[int]:
        with self.lock:
            return [
                int(r["start_at"])
                for r in self.requests
                if r["path"] == "/rest/api/3/project/search"
            ]


class _JiraStubHandler(BaseHTTPRequestHandler):
    server_version = "OmniJiraStub/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    @property
    def state(self) -> _JiraStubState:
        return self.server.stub_state  # type: ignore[attr-defined,no-any-return]

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/rest/api/3/myself":
            self.state.record({"path": path})
            self._send_json(
                200,
                {
                    "accountId": "fh-acc-1",
                    "displayName": "FH Bot",
                    "emailAddress": "fh-bot@example.com",
                },
            )
            return
        if path == "/rest/api/3/project/search":
            start_at = int((query.get("startAt") or ["0"])[0])
            self.state.record({"path": path, "start_at": start_at})
            if start_at == 0:
                self._send_json(
                    200,
                    {
                        "values": _PAGE_ONE,
                        "isLast": False,
                        "startAt": 0,
                        "maxResults": 50,
                        "total": 3,
                    },
                )
            else:
                self._send_json(
                    200,
                    {
                        "values": _PAGE_TWO,
                        "isLast": True,
                        "startAt": start_at,
                        "maxResults": 50,
                        "total": 3,
                    },
                )
            return
        if path == "/rest/api/3/project/ACM/statuses":
            self.state.record({"path": path})
            self._send_json(200, _STATUS_GROUPS)
            return
        self._send_json(404, {"error": "not found"})


@pytest.fixture()
def jira_stub() -> Iterator[tuple[str, _JiraStubState]]:
    """Loopback-only stub Jira on a free port; torn down after the test."""
    state = _JiraStubState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JiraStubHandler)
    server.stub_state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


@pytest.fixture()
def auth_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return {"X-Session-Token": token.load_or_create_token()}


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def stub_env(
    jira_stub: tuple[str, _JiraStubState], monkeypatch: pytest.MonkeyPatch
) -> tuple[str, _JiraStubState]:
    base_url, state = jira_stub
    monkeypatch.setenv("JIRA_BASE_URL", base_url)
    monkeypatch.setenv("JIRA_EMAIL", "fh-bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "fh-fake-token")
    return base_url, state


def test_projects_paginates_fully_across_stub_pages(
    client: TestClient,
    auth_headers: dict[str, str],
    stub_env: tuple[str, _JiraStubState],
) -> None:
    _, state = stub_env
    response = client.get("/api/jira/projects", headers=auth_headers)
    assert response.status_code == 200, response.text
    projects = response.json()
    assert [p["key"] for p in projects] == ["ALPHA", "BRAVO", "CHARLIE"]
    assert all(set(p) == {"id", "key", "name", "projectTypeKey"} for p in projects)
    # Far side: the client actually walked both pages (startAt 0 then 2).
    assert state.search_pages() == [0, 2]


def test_statuses_dedup_by_name_across_issue_type_groups(
    client: TestClient,
    auth_headers: dict[str, str],
    stub_env: tuple[str, _JiraStubState],
) -> None:
    response = client.get("/api/jira/projects/ACM/statuses", headers=auth_headers)
    assert response.status_code == 200, response.text
    statuses = response.json()
    names = [s["name"] for s in statuses]
    assert len(names) == len(set(names)), f"duplicate status names surfaced: {names}"
    assert set(names) == {"To Do", "In Progress", "Done", "Blocked"}
    # Dedup keeps the FIRST occurrence of each name (Task group's ids).
    by_name = {s["name"]: s for s in statuses}
    assert by_name["To Do"]["id"] == "10"
    assert by_name["Done"]["id"] == "12"
    assert by_name["To Do"]["statusCategoryKey"] == "new"


def test_health_ok_shape_against_stub(
    client: TestClient,
    auth_headers: dict[str, str],
    stub_env: tuple[str, _JiraStubState],
) -> None:
    response = client.get("/api/jira/health", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "displayName": "FH Bot",
        "accountId": "fh-acc-1",
    }


def test_health_unconfigured_returns_503_with_code(
    client: TestClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    response = client.get("/api/jira/health", headers=auth_headers)
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == "jira_unconfigured"


class _RecordingTransport(httpx.BaseTransport):
    """tests/connectors/test_jira_retry.py idiom, plus a shared event timeline."""

    def __init__(self, handler: Any, timeline: list[tuple[str, Any]]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler
        self._timeline = timeline

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self._timeline.append(("request", request.url.path))
        return self._handler(request)


def test_per_issue_rate_limit_pauses_next_send_to_that_issue_only() -> None:
    """429 + RateLimit-Reason: jira-per-issue-on-write → the client sleeps
    BEFORE the next send to that issue; other issues are not paused."""
    timeline: list[tuple[str, Any]] = []
    hits = {"apu9": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/issue/ACM-9"):
            hits["apu9"] += 1
            if hits["apu9"] == 1:
                return httpx.Response(
                    429,
                    headers={
                        "Retry-After": "2",
                        "RateLimit-Reason": "jira-per-issue-on-write",
                    },
                    text="per-issue",
                )
            return httpx.Response(
                200, json={"id": "9", "key": "ACM-9", "fields": {"summary": "x"}}
            )
        return httpx.Response(
            200, json={"id": "8", "key": "ACM-8", "fields": {"summary": "y"}}
        )

    transport = _RecordingTransport(handler, timeline)

    def record_sleep(seconds: float) -> None:
        timeline.append(("sleep", seconds))

    http = httpx.Client(transport=transport)
    jira = JiraClient(
        base_url="https://initech-team.atlassian.net",
        email="bot@example.com",
        api_token="tok",
        client=http,
        sleep=record_sleep,
    )
    # First call: 429 → retry sleep → 200; backoff recorded for ACM-9.
    jira.get_issue("ACM-9")
    assert jira._issue_backoff_until.get("ACM-9", 0.0) > 0.0  # noqa: SLF001
    assert jira._quota_pause_until == 0.0  # noqa: SLF001 — not client-wide

    # A DIFFERENT issue is not paused: its send has no preceding sleep.
    before = len(timeline)
    jira.get_issue("ACM-8")
    apu8_events = timeline[before:]
    assert apu8_events[0] == ("request", "/rest/api/3/issue/ACM-8")

    # Same issue again: the pause is enforced BEFORE the send.
    before = len(timeline)
    jira.get_issue("ACM-9")
    apu9_events = timeline[before:]
    assert apu9_events[0][0] == "sleep", (
        f"expected a sleep before the next ACM-9 send, got {apu9_events}"
    )
    assert apu9_events[0][1] > 0.0
    assert apu9_events[1] == ("request", "/rest/api/3/issue/ACM-9")
    jira.close()


def test_no_public_write_surface() -> None:
    """JiraClient exposes no public field-PUT and the app mounts no jira writes."""
    assert not hasattr(JiraClient, "put_issue_fields"), (
        "public put_issue_fields would reopen a write surface privatized by the "
        "broker/security boundary (the internal seam is _put_issue_fields)"
    )
    verbs = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
    paths = app.openapi()["paths"]
    jira_paths = {p: spec for p, spec in paths.items() if p.startswith("/api/jira")}
    assert jira_paths, "no /api/jira paths mounted — surface disappeared"
    for path, spec in jira_paths.items():
        methods = {m for m in spec if m in verbs}
        assert methods == {"get"}, f"{path} exposes non-GET methods: {methods}"
