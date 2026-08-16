"""Fixtures for W3 hierarchy + conversation tests.

Like the W2 project suite, these run against a real in-memory ``SqliteStore`` so
the migration-031 schema (parent_project_id + conversations) is exercised end to
end rather than faked.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.conversations import ConversationStore
from omniagentos.db.store import SqliteStore
from omniagentos.projects import ProjectStore


@pytest.fixture
def store() -> SqliteStore:
    return SqliteStore(":memory:")


@pytest.fixture
def project_store(store: SqliteStore) -> ProjectStore:
    return ProjectStore(store)


@pytest.fixture
def conversation_store(store: SqliteStore) -> ConversationStore:
    return ConversationStore(store)


@pytest.fixture
def asgi_client(store: SqliteStore) -> Iterator[httpx.AsyncClient]:
    app.dependency_overrides[get_store] = lambda: store
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
