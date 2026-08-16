from __future__ import annotations

from collections.abc import Generator

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from tests.api.fake_store import FakeStore


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def asgi_client(store: FakeStore) -> Generator[httpx.AsyncClient, None, None]:
    app.dependency_overrides[get_store] = lambda: store
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
