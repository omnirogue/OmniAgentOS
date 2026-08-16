"""Fixtures for W2 project tests.

Unlike the core control-plane suite (which uses the in-memory FakeStore), the
project router composes a :class:`ProjectStore` over the injected store and must
run against the real SQLite schema, so these tests inject a real in-memory
:class:`SqliteStore`.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.db.store import SqliteStore
from omniagentos.projects import ProjectStore


@pytest.fixture
def store() -> SqliteStore:
    return SqliteStore(":memory:")


@pytest.fixture
def project_store(store: SqliteStore) -> ProjectStore:
    return ProjectStore(store)


@pytest.fixture
def asgi_client(store: SqliteStore) -> Iterator[httpx.AsyncClient]:
    app.dependency_overrides[get_store] = lambda: store
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
