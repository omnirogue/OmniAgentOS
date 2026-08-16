"""POST /api/comms/inbound — auth / size / rate-limit / dedupe matrix.

Exercises the REAL FastAPI app + a real tmp-path SQLite store (see conftest.py),
proving the webhook path never needs the knowledge subsystem or the network.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator

import httpx
import pytest

from omniagentos.api.routes import comms as comms_routes
from omniagentos.steward.store import StewardStore
from tests.comms.conftest import TEST_SECRET_ENV, TEST_SOURCE


def _post(
    client: httpx.AsyncClient,
    *,
    source: str = TEST_SOURCE,
    token: str | None = "correct-token",
    json: dict[str, object] | None = None,
    content: bytes | AsyncIterator[bytes] | None = None,
) -> httpx.Response:
    headers = {"X-Comms-Token": token} if token is not None else {}
    kwargs: dict[str, object] = {"params": {"source": source}, "headers": headers}
    if content is not None:
        kwargs["content"] = content
    else:
        kwargs["json"] = (
            json
            if json is not None
            else {"from": "a@example.com", "subject": "hi", "body": "hello"}
        )
    return asyncio.run(client.post("/api/comms/inbound", **kwargs))


def test_unknown_source_is_401(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TEST_SECRET_ENV, "correct-token")
    response = _post(asgi_client, source="not-configured")
    assert response.status_code == 401


def test_missing_token_is_401(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TEST_SECRET_ENV, "correct-token")
    response = _post(asgi_client, token=None)
    assert response.status_code == 401


def test_wrong_token_is_401(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TEST_SECRET_ENV, "correct-token")
    response = _post(asgi_client, token="wrong-token")
    assert response.status_code == 401


def test_secret_env_unset_is_401(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TEST_SECRET_ENV, raising=False)
    response = _post(asgi_client, token="anything")
    assert response.status_code == 401


def test_success_then_dedupe(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TEST_SECRET_ENV, "correct-token")
    payload = {"external_id": "ext-1", "from": "a@example.com", "subject": "hi", "body": "hello"}
    first = _post(asgi_client, json=payload)
    assert first.status_code == 202
    body = first.json()
    assert body["created"] is True
    assert isinstance(body["id"], int)

    second = _post(asgi_client, json=payload)
    assert second.status_code == 200
    assert second.json() == {"id": body["id"], "created": False}


def test_stored_row_matches_normalized_fields(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, steward: StewardStore
) -> None:
    monkeypatch.setenv(TEST_SECRET_ENV, "correct-token")
    payload = {
        "external_id": "ext-full",
        "from": "vip@example.com",
        "subject": "Urgent",
        "body": "please help",
    }
    response = _post(asgi_client, json=payload)
    row = steward.get_comms_message(response.json()["id"])
    assert row is not None
    assert row["source"] == TEST_SOURCE
    assert row["sender"] == "vip@example.com"
    assert row["subject"] == "Urgent"
    assert row["body_text"] == "please help"
    assert row["kb_status"] == "none"


def test_body_over_declared_content_length_is_413(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TEST_SECRET_ENV, "correct-token")
    oversized = b'{"body": "' + b"x" * 2000 + b'"}'
    response = _post(asgi_client, content=oversized)
    assert response.status_code == 413


async def _chunked(payload: bytes) -> AsyncIterator[bytes]:
    # Yield in small pieces with no Content-Length header, forcing the route to
    # rely on its own streaming cap rather than a (possibly absent/false) header.
    for start in range(0, len(payload), 64):
        yield payload[start : start + 64]


def test_body_over_limit_without_content_length_header_is_413(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TEST_SECRET_ENV, "correct-token")
    oversized = b'{"body": "' + b"x" * 2000 + b'"}'
    response = _post(asgi_client, content=_chunked(oversized))
    assert response.status_code == 413


def test_body_within_limit_succeeds(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TEST_SECRET_ENV, "correct-token")
    small = b'{"external_id": "small-1", "from": "a@example.com", "subject": "s", "body": "ok"}'
    response = _post(asgi_client, content=small)
    assert response.status_code == 202


def test_rate_limit_returns_429_after_budget_exhausted(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TEST_SECRET_ENV, "correct-token")
    # steward_config fixture sets rate_limit_per_minute=3.
    statuses = []
    for index in range(4):
        response = _post(
            asgi_client, json={"external_id": f"rl-{index}", "from": "a@example.com", "body": "x"}
        )
        statuses.append(response.status_code)
    assert statuses[:3] == [202, 202, 202]
    assert statuses[3] == 429


def test_inbound_route_never_imports_knowledge_subsystem() -> None:
    import_lines = [
        line
        for line in inspect.getsource(comms_routes).splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    assert not any("knowledge" in line for line in import_lines)
    assert not any("ingest_episode" in line for line in import_lines)


def test_invalid_json_body_is_400(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TEST_SECRET_ENV, "correct-token")
    response = _post(asgi_client, content=b"not json at all")
    assert response.status_code == 400


def test_get_messages_and_sources_round_trip(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, steward: StewardStore
) -> None:
    monkeypatch.setenv(TEST_SECRET_ENV, "correct-token")
    _post(
        asgi_client,
        json={"external_id": "list-1", "from": "a@example.com", "subject": "s1", "body": "b1"},
    )
    _post(
        asgi_client,
        json={"external_id": "list-2", "from": "b@example.com", "subject": "s2", "body": "b2"},
    )

    listed = asyncio.run(asgi_client.get("/api/comms/messages")).json()
    assert len(listed) == 2
    assert listed[0]["external_id"] == "list-2"  # newest first

    one = asyncio.run(asgi_client.get(f"/api/comms/messages/{listed[0]['id']}")).json()
    assert one["external_id"] == "list-2"

    missing = asyncio.run(asgi_client.get("/api/comms/messages/999999"))
    assert missing.status_code == 404

    filtered = asyncio.run(asgi_client.get("/api/comms/messages", params={"q": "s1"})).json()
    assert [row["external_id"] for row in filtered] == ["list-1"]

    # comms_sources rows come from pollers/operators, not the inbound webhook path;
    # seed one directly through the (shared) store to prove GET /sources surfaces it.
    steward.upsert_comms_source(TEST_SOURCE, "webhook", status="active")
    sources = asyncio.run(asgi_client.get("/api/comms/sources")).json()
    assert any(row["name"] == TEST_SOURCE for row in sources)
