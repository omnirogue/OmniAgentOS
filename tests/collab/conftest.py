"""Fixtures for Collab API tests (mirrors tests/chats/conftest.py)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from omniagentos.api.deps import get_policy_config, get_store
from omniagentos.api.main import app
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.store import CollabStore
from omniagentos.db.store import SqliteStore
from omniagentos.policy import PolicyConfig, load_policy
from tests.support.db_template import make_store


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "collab_test.db"


@pytest.fixture
def collab_store(tmp_db_path: Path) -> CollabStore:
    return make_store(CollabStore, tmp_db_path)


@pytest.fixture
def store(collab_store: CollabStore) -> SqliteStore:
    return collab_store._store


@pytest.fixture
def policy_config() -> PolicyConfig:
    return load_policy()


@pytest.fixture
def asgi_client(
    store: SqliteStore,
    collab_store: CollabStore,
    policy_config: PolicyConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[httpx.AsyncClient]:
    previous_overrides = dict(app.dependency_overrides)
    monkeypatch.setenv("OMNIAGENTOS_DISCOVER_EXTERNAL", "0")
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_collab_store] = lambda: collab_store
    app.dependency_overrides[get_policy_config] = lambda: policy_config

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client
    finally:
        app.dependency_overrides = previous_overrides
