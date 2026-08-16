"""Body-less POSTs to the three memlife decision routes.

A body-less POST is an explicitly supported call — the signature is
``req: MemlifeDecisionRequest | None = None``. Commit ``a2aa324f`` left
``or DecisionRequest()`` (undefined name) on that path; every empty-body
POST raised NameError. These tests pin that surface so it cannot return.

Against a present store with a missing candidate id, the outcome is 404 —
not 500, and not a NameError.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.db.store import SqliteStore
from omniagentos.memlife.store import MemlifeStore
from tests.support.db_template import make_store

ENV_STORE = "OMNIAGENTOS_MEMLIFE_STORE"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "bodyless.db")


@pytest.fixture
def memlife_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "memlife_store"
    MemlifeStore(root).ensure_layout()
    monkeypatch.setenv(ENV_STORE, str(root))
    import omniagentos.api.routes.memlife as memlife_routes

    if hasattr(memlife_routes, "_clear_store_root_cache"):
        memlife_routes._clear_store_root_cache()
    return root


@pytest.fixture
def client(database: SqliteStore) -> Iterator[httpx.AsyncClient]:
    app.dependency_overrides[get_store] = lambda: database
    c = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        yield c
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("action", ["graduate", "reject", "reopen"])
def test_bodyless_post_missing_candidate_is_404(
    database: SqliteStore,
    memlife_root: Path,
    client: httpx.AsyncClient,
    action: str,
) -> None:
    """Empty body + unknown id → 404 (and no NameError on DecisionRequest)."""
    assert memlife_root.is_dir()
    # No Content-Type / no JSON body — the body-less path.
    response = _run(client.post(f"/api/memlife/cand_does_not_exist/{action}"))
    assert response.status_code == 404, response.text
