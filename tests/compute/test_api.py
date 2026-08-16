"""HTTP surface: the gated /api/compute/estate read, and its 15s cache.

Same ASGI-against-httpx pattern as tests/testobs/test_api.py. No store
override is needed — this handler never touches the control-plane database.
It IS session-token gated (SEC-005), so every request carries the header;
``real_auth`` opts out of the suite-wide app-level bypass.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from omniagentos.api.main import app
from omniagentos.api.routes import compute as compute_route
from omniagentos.compute import readers
from tests.compute.conftest import (
    POOL_STATUS_SAMPLE_BYTES,
    RUNNERS_SAMPLE_STDOUT,
    fake_fetch,
    fake_runner,
)

pytestmark = pytest.mark.real_auth


@pytest.fixture(autouse=True)
def _clear_compute_cache() -> Iterator[None]:
    """The route's single-slot cache must not leak state between tests."""
    compute_route._clear_cache()
    yield
    compute_route._clear_cache()


def _get(path: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    async def request() -> tuple[int, Any]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(path, headers=headers or {})
            return response.status_code, response.json()

    return asyncio.run(request())


def test_estate_requires_the_session_token() -> None:
    status, body = _get("/api/compute/estate")
    assert status == 401
    assert body["error"]["code"] == "unauthorized"


def test_estate_wire_shape_and_local_always_available(
    auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with pool/runners both absent, the envelope is well-formed and 200."""
    monkeypatch.delenv("WQ_TOKEN", raising=False)
    monkeypatch.setattr(readers, "CONNECTIONS_ENV", readers.CONNECTIONS_ENV.parent / "nope.env")
    monkeypatch.setattr(readers, "_resolve_gh", lambda: None)

    status, body = _get("/api/compute/estate", auth_headers)
    assert status == 200
    assert set(body) == {"generated_at", "pool", "runners", "local"}
    assert body["generated_at"].endswith("Z")
    assert body["pool"]["available"] is False and body["pool"]["reason"]
    assert body["runners"]["available"] is False and body["runners"]["reason"]
    assert body["local"]["available"] is True
    assert body["local"]["hostname"]


def test_estate_survives_a_raising_collector(
    auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One collector raising must not 500 the endpoint or take the other two down."""

    def raising_local() -> dict[str, Any]:
        raise OSError("loadavg unobtainable")

    monkeypatch.setattr(compute_route, "read_local", raising_local)
    monkeypatch.setattr(readers, "_resolve_gh", lambda: None)  # runners: absent, not raising

    status, body = _get("/api/compute/estate", auth_headers)
    assert status == 200
    assert body["local"]["available"] is False
    assert set(body["local"]) == {"available", "reason"}
    assert "Error" not in body["local"]["reason"] and "/" not in body["local"]["reason"]
    # The two collectors that did NOT raise still produced their own envelopes.
    assert "available" in body["pool"] and "available" in body["runners"]


def test_estate_response_never_leaks_the_pool_token(
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    connections_env: Callable[[str], None],
) -> None:
    token = "wq-secret-xyz789"
    connections_env(f"WQ_TOKEN={token}\n")

    def real_pool(**_: Any) -> dict[str, Any]:
        return readers.read_pool(fetch=fake_fetch(POOL_STATUS_SAMPLE_BYTES))

    def real_runners(**_: Any) -> dict[str, Any]:
        return readers.read_runners(runner=fake_runner(stdout=RUNNERS_SAMPLE_STDOUT))

    monkeypatch.setattr(compute_route, "read_pool", real_pool)
    monkeypatch.setattr(compute_route, "read_runners", real_runners)

    status, body = _get("/api/compute/estate", auth_headers)
    assert status == 200
    assert body["pool"]["available"] is True  # token really was used, not silently skipped
    assert token not in json.dumps(body)


def test_estate_caches_within_ttl_and_never_caches_a_failure(
    auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"pool": 0, "runners": 0}

    def counting_pool(**_: Any) -> dict[str, Any]:
        calls["pool"] += 1
        return readers.read_pool(fetch=fake_fetch(POOL_STATUS_SAMPLE_BYTES), token="tok")

    def counting_runners(**_: Any) -> dict[str, Any]:
        calls["runners"] += 1
        return readers.read_runners(runner=fake_runner(stdout=RUNNERS_SAMPLE_STDOUT))

    monkeypatch.setattr(compute_route, "read_pool", counting_pool)
    monkeypatch.setattr(compute_route, "read_runners", counting_runners)

    status1, body1 = _get("/api/compute/estate", auth_headers)
    status2, body2 = _get("/api/compute/estate", auth_headers)
    assert status1 == status2 == 200
    assert body1["pool"]["available"] is True
    # Second call within the 15s TTL must not re-fetch either source.
    assert calls == {"pool": 1, "runners": 1}
    # Same cached generated_at proves it was served from cache, not re-built.
    assert body1["generated_at"] == body2["generated_at"]


def test_estate_failure_is_never_cached(
    auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"pool": 0}

    def failing_pool(**_: Any) -> dict[str, Any]:
        calls["pool"] += 1
        return {"available": False, "reason": "compute pool is not reachable right now"}

    monkeypatch.setattr(compute_route, "read_pool", failing_pool)
    monkeypatch.setattr(readers, "_resolve_gh", lambda: None)

    _get("/api/compute/estate", auth_headers)
    _get("/api/compute/estate", auth_headers)
    # A snapshot where any source failed is not a success -- every call re-fetches.
    assert calls["pool"] == 2
