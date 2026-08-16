"""Shared fixtures for the comms test suite.

Tests exercise the REAL app + a real, tmp-path SQLite store (never the in-memory
``FakeStore`` used by tests/api/**) so ``StewardStore`` construction and the
`comms_messages`/`comms_sources` tables behave exactly as they would in
production.

PostgreSQL-specific bootstrap (needed only by the flagship
test_injection_e2e.py) intentionally lives in that module, not here — these
fixtures must stay independent of PG reachability so the rest of this suite
(normalize/curate/webhook/pollers/extract_batch, all pure-SQLite) never skips
or fails because Postgres happens to be down.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes import comms as comms_routes
from omniagentos.db.store import SqliteStore
from omniagentos.steward.config import CommsConfig, InboundSourceCfg, StewardConfig
from omniagentos.steward.store import StewardStore
from tests.support.db_template import make_store

TEST_SECRET_ENV = "COMMS_TEST_TOKEN_A"
TEST_SOURCE = "testsource"


@pytest.fixture
def sqlite_store(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "comms.db")


@pytest.fixture
def steward(sqlite_store: SqliteStore) -> StewardStore:
    return StewardStore(sqlite_store)


@pytest.fixture
def steward_config() -> StewardConfig:
    cfg = StewardConfig()
    cfg.comms = CommsConfig(
        inbound_max_bytes=1024,
        rate_limit_per_minute=3,
        sources={TEST_SOURCE: InboundSourceCfg(secret_env=TEST_SECRET_ENV)},
    )
    return cfg


@pytest.fixture
def asgi_client(
    sqlite_store: SqliteStore, steward_config: StewardConfig
) -> Generator[httpx.AsyncClient, None, None]:
    app.dependency_overrides[get_store] = lambda: sqlite_store
    app.dependency_overrides[comms_routes.get_steward_config] = lambda: steward_config
    comms_routes.reset_rate_limits()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        comms_routes.reset_rate_limits()
