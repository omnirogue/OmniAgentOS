"""Regression coverage for hostile control-plane request inputs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.types import Message

from omniagentos.api.deps import get_store
from omniagentos.api.main import (
    _HTTP_ERROR_CODES,
    _BodySizeLimitMiddleware,
    app,
    require_session_token,
)
from omniagentos.api.routes.categories import get_longhaul_store
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.api.routes.sessions import _authorized
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import ErrorCode
from omniagentos.db.migrate import migrate
from omniagentos.longhaul import LonghaulStore


@pytest.fixture()
def client(tmp_path: Path) -> Any:
    """Isolated app client for board and category write regressions."""
    db_path = str(tmp_path / "hostile-input.db")
    migrate(db_path)
    collab = CollabStore(db_path)
    longhaul = LonghaulStore(db_path)
    app.dependency_overrides[get_store] = lambda: collab._store
    app.dependency_overrides[get_collab_store] = lambda: collab
    app.dependency_overrides[get_longhaul_store] = lambda: longhaul
    app.dependency_overrides[require_session_token] = lambda: None
    app.dependency_overrides[_authorized] = lambda: None
    api_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    try:
        yield api_client
    finally:
        app.dependency_overrides.clear()
        asyncio.run(api_client.aclose())
        longhaul.close()


def _call(coro: Any) -> httpx.Response:
    return asyncio.run(coro)


def test_oversized_json_board_title_is_rejected_before_classification(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A giant JSON title must not reach the synchronous classifier."""
    from omniagentos.orgdims.service import OrgDimsService

    classified = False

    def classify_board_task(self: OrgDimsService, **_: Any) -> None:
        nonlocal classified
        classified = True

    monkeypatch.setattr(OrgDimsService, "classify_board_task", classify_board_task)
    response = _call(client.post("/api/collab/board", json={"title": "x" * (17 * 1024 * 1024)}))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert not classified


@pytest.mark.parametrize("headers", [{"content-type": "text/plain"}, {}])
def test_oversized_non_json_body_is_rejected(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    """Every HTTP body is capped even when FastAPI reads it as non-JSON."""
    response = _call(
        client.post("/api/categories", content=b"x" * (17 * 1024 * 1024), headers=headers)
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_multipart_header_does_not_expand_limit_for_json_only_route(
    client: httpx.AsyncClient,
) -> None:
    """A caller cannot claim multipart to bypass a JSON-only route's cap."""
    response = _call(
        client.post(
            "/api/collab/board",
            content=b"x" * (17 * 1024 * 1024),
            headers={"content-type": "multipart/form-data; boundary=zz"},
        )
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_oversize_rejection_keeps_cors_headers(client: httpx.AsyncClient) -> None:
    """The outer CORS layer decorates middleware-generated 413 responses."""
    response = _call(
        client.post(
            "/api/categories",
            json={"name": "x" * (17 * 1024 * 1024)},
            headers={"origin": "http://localhost:3003"},
        )
    )

    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == "http://localhost:3003"


def test_body_limit_never_starts_a_second_response_after_downstream_start() -> None:
    """An over-limit late chunk keeps its first response and terminates it cleanly."""
    sent: list[Message] = []
    requests = iter(
        [
            {"type": "http.request", "body": b"ok", "more_body": True},
            {"type": "http.request", "body": b"too-large", "more_body": False},
        ]
    )

    async def receive() -> dict[str, Any]:
        return next(requests)

    async def send(message: Message) -> None:
        sent.append(message)

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        del scope
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()
        await send({"type": "http.response.body", "body": b"ignored"})

    middleware = _BodySizeLimitMiddleware(downstream, max_body_bytes=2)
    _call(
        middleware(
            {"type": "http", "headers": [(b"content-type", b"application/json")]},
            receive,
            send,
        )
    )

    assert sent == [
        {"type": "http.response.start", "status": 200, "headers": []},
        {"type": "http.response.body", "body": b"", "more_body": False},
    ]


def test_overlimit_downstream_exception_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """An exception suppressed after refusal remains observable to operators."""
    sent: list[Message] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"too-large", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        del scope, send
        await receive()
        raise RuntimeError("downstream failed after body refusal")

    middleware = _BodySizeLimitMiddleware(downstream, max_body_bytes=2)
    _call(middleware({"type": "http", "headers": []}, receive, send))

    assert "suppressed downstream error on over-limit body" in caplog.text
    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]


def test_deep_validation_failure_does_not_echo_raw_input(client: httpx.AsyncClient) -> None:
    """Validation errors must not serialize an attacker-controlled nested body."""
    value: object = "not a title"
    for _ in range(100):
        value = {"nested": value}

    response = _call(client.post("/api/collab/board", json={"title": value}))

    assert response.status_code == 422
    detail = response.json()["error"]["detail"]
    assert detail[0]["loc"] == ["body", "title"]
    assert "input" not in detail[0]


def test_shallow_validation_failure_keeps_field_detail(client: httpx.AsyncClient) -> None:
    """Sanitizing validation errors retains useful, bounded field diagnostics."""
    response = _call(client.post("/api/collab/board", json={"title": 1}))

    assert response.status_code == 422
    detail = response.json()["error"]["detail"]
    assert detail[0]["loc"] == ["body", "title"]
    assert detail[0]["msg"]
    assert detail[0]["type"] == "string_type"


def test_validation_error_clips_attacker_controlled_location_item(client: httpx.AsyncClient) -> None:
    """Forbidden extra keys cannot inflate the response through validation locs."""
    huge_key = "x" * 20_002
    response = _call(client.post("/api/projects", json={"name": "project", huge_key: True}))

    assert response.status_code == 422
    detail = response.json()["error"]["detail"]
    extra = next(error for error in detail if error["type"] == "extra_forbidden")
    assert len(extra["loc"][-1]) <= 200
    assert len(extra["type"]) <= 512


def test_oversized_board_title_is_rejected_before_persistence(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Board titles are display text, not an unbounded persistence channel."""
    from omniagentos.collab.store import CollabStore

    persisted = False

    def create_board_task(self: CollabStore, task: Any) -> None:
        del self, task
        nonlocal persisted
        persisted = True

    monkeypatch.setattr(CollabStore, "create_board_task", create_board_task)
    response = _call(client.post("/api/collab/board", json={"title": "x" * 100_000}))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation"
    assert not persisted


def test_oversized_category_name_is_rejected_before_persistence(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Category names are display text, not an unbounded persistence channel."""
    from omniagentos.longhaul.store import LonghaulStore

    persisted = False

    def create_category(self: LonghaulStore, *args: Any, **kwargs: Any) -> Any:
        del self, args, kwargs
        nonlocal persisted
        persisted = True
        raise AssertionError("oversized category name reached persistence")

    monkeypatch.setattr(LonghaulStore, "create_category", create_category)
    response = _call(client.post("/api/categories", json={"name": "x" * 100_000}))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation"
    assert not persisted


def test_reasonably_long_board_title_and_category_name_are_accepted(
    client: httpx.AsyncClient,
) -> None:
    """The finite display-text bounds remain generous for normal use."""
    board = _call(client.post("/api/collab/board", json={"title": "b" * 512}))
    category = _call(client.post("/api/categories", json={"name": "c" * 512}))

    assert board.status_code == 201
    assert category.status_code == 201


def test_method_not_allowed_uses_a_client_error_code(client: httpx.AsyncClient) -> None:
    """A normal protocol 405 must not be reported as an internal failure."""
    response = _call(client.post("/api/health"))

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_http_error_codes_and_contract_enum_cover_emitted_errors() -> None:
    expected = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        405: "method_not_allowed",
        413: "request_too_large",
        415: "unsupported_media_type",
        429: "rate_limited",
        503: "unavailable",
    }

    assert {status: _HTTP_ERROR_CODES[status] for status in expected} == expected
    assert {ErrorCode(code).value for code in expected.values()} == set(expected.values())


def test_category_create_distinguishes_new_idempotent_and_slug_collision(
    client: httpx.AsyncClient,
) -> None:
    """A slug collision must never return an unrelated category as newly created."""
    created = _call(client.post("/api/categories", json={"name": "Design"}))
    same_name = _call(client.post("/api/categories", json={"name": "  Design  "}))
    collision = _call(client.post("/api/categories", json={"name": "design!!!"}))

    assert created.status_code == 201
    assert same_name.status_code == 200
    assert same_name.json()["id"] == created.json()["id"]
    assert collision.status_code == 409
    assert collision.json()["error"]["code"] == "conflict"


def test_category_route_and_store_share_the_canonical_slugifier() -> None:
    """The route's conflict check and store deduplication cannot drift apart."""
    from omniagentos.api.routes.categories import slugify as route_slugify
    from omniagentos.longhaul.store import LonghaulStore
    from omniagentos.longhaul.store import slugify as store_slugify

    assert route_slugify is store_slugify
    assert LonghaulStore._slugify("Design Team") == store_slugify("Design Team")


@pytest.mark.parametrize("name", ["!!!", "🤖🤖"])
def test_category_name_that_slugifies_to_empty_is_rejected_before_store(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Distinct hostile names must not alias the same empty-slug row."""
    from omniagentos.longhaul.store import LonghaulStore

    reached_store = False

    def create_category(self: LonghaulStore, *args: Any, **kwargs: Any) -> Any:
        del self, args, kwargs
        nonlocal reached_store
        reached_store = True
        raise AssertionError("empty slug reached category create-or-get")

    monkeypatch.setattr(LonghaulStore, "create_category", create_category)
    response = _call(client.post("/api/categories", json={"name": name}))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation"
    assert not reached_store
