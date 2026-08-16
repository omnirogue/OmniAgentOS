"""Tests for orchestration resume endpoints and lifespan hooks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import omniagentos.swarm.scheduler as scheduler_module
from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from tests.api.fake_store import FakeStore


@pytest.fixture
def collab_store(tmp_path: Path) -> CollabStore:
    """Fixture for a CollabStore backed by a temp database."""
    db = str(tmp_path / "collab.db")
    return CollabStore(db)


@pytest.fixture
def collab_asgi_client(store: FakeStore, collab_store: CollabStore) -> httpx.AsyncClient:
    """An ASGI client with both store and collab_store overrides."""
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_collab_store] = lambda: collab_store
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client
    finally:
        app.dependency_overrides.clear()
        asyncio.run(client.aclose())


def test_retry_board_task_success_200(
    collab_asgi_client: httpx.AsyncClient,
    collab_store: CollabStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test successful retry endpoint returns 200 with correct response."""
    # Create a board task
    task = BoardTask(title="Test Task", description="To retry")
    collab_store.create_board_task(task)
    task_id = task.id

    # Mock resume_orchestration to return success
    mock_result = {"run_id": "run_123", "resumed": True, "reason": "manual_retry"}

    def mock_resume(
        board_task_id: str, *, store: object, collab_store: object, manual: bool = False
    ) -> dict:
        assert board_task_id == task_id
        assert manual is True
        assert store is not None
        assert collab_store is not None
        return mock_result

    monkeypatch.setattr(
        "omniagentos.api.routes.intake._resolve_resume_orchestration",
        lambda: mock_resume,
    )

    # Call the endpoint
    response = asyncio.run(collab_asgi_client.post(f"/api/board/{task_id}/retry"))

    assert response.status_code == 200
    assert response.json() == mock_result


def test_retry_board_task_not_found_404(
    collab_asgi_client: httpx.AsyncClient,
) -> None:
    """Test retry endpoint returns 404 for unknown board task."""
    response = asyncio.run(collab_asgi_client.post("/api/board/btk_unknown/retry"))

    assert response.status_code == 404
    resp_json = response.json()
    assert resp_json["error"]["code"] == "not_found"
    assert "not found" in resp_json["error"]["message"].lower()


def test_retry_board_task_conflict_409(
    collab_asgi_client: httpx.AsyncClient,
    collab_store: CollabStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test retry endpoint returns 409 when task is not resumable."""
    # Create a board task
    task = BoardTask(title="Not Resumable Task", description="Longhaul task")
    collab_store.create_board_task(task)
    task_id = task.id

    # Mock resume_orchestration to raise ValueError (not resumable)
    def mock_resume_error(
        board_task_id: str, *, store: object, collab_store: object, manual: bool = False
    ) -> dict:
        raise ValueError("conductor live / not resumable / longhaul lane")

    monkeypatch.setattr(
        "omniagentos.api.routes.intake._resolve_resume_orchestration",
        lambda: mock_resume_error,
    )

    # Call the endpoint
    response = asyncio.run(collab_asgi_client.post(f"/api/board/{task_id}/retry"))

    assert response.status_code == 409
    resp_json = response.json()
    assert resp_json["error"]["code"] == "conflict"


def test_retry_board_task_lookup_error_404(
    collab_asgi_client: httpx.AsyncClient,
    collab_store: CollabStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test retry endpoint returns 404 when resume_orchestration raises LookupError."""
    # Create a board task
    task = BoardTask(title="Unknown Run Task", description="Missing run")
    collab_store.create_board_task(task)
    task_id = task.id

    # Mock resume_orchestration to raise LookupError
    def mock_resume_lookup_error(
        board_task_id: str, *, store: object, collab_store: object, manual: bool = False
    ) -> dict:
        raise LookupError("unknown board_task_id")

    monkeypatch.setattr(
        "omniagentos.api.routes.intake._resolve_resume_orchestration",
        lambda: mock_resume_lookup_error,
    )

    # Call the endpoint
    response = asyncio.run(collab_asgi_client.post(f"/api/board/{task_id}/retry"))

    assert response.status_code == 404
    resp_json = response.json()
    assert resp_json["error"]["code"] == "not_found"


def test_lifespan_resume_enabled_calls_resume_orphaned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Test lifespan hook calls resume_orphaned_orchestrations when enabled."""
    monkeypatch.setenv("OMNIAGENTOS_ORCH_RESUME_ON_STARTUP", "1")
    monkeypatch.setenv("OMNIAGENTOS_ORCH_RESUME_STARTUP_DELAY_SECONDS", "0")
    monkeypatch.setenv("OMNIAGENTOS_SWARM_RESUME_ON_STARTUP", "0")

    import asyncio
    import threading

    done = threading.Event()
    call_count = 0
    call_args: dict = {}

    def mock_resume_orphaned(*, store: object, collab_store: object, limit: int = 8):
        nonlocal call_count, call_args
        call_count += 1
        call_args = {"store": store, "collab_store": collab_store, "limit": limit}
        done.set()
        return ["run_123", "run_456"]

    # Patch the lazy resolver, and stub the store getters the daemon lazy-imports
    # at call time: a cold get_store()+get_collab_store() migrates the session DB
    # twice (~85 scripts each), which outlives any fixed grace window and warms
    # process-wide lru_cache singletons other tests then depend on.
    with (
        patch(
            "omniagentos.api.main._resolve_resume_orphaned_orchestrations",
            lambda: mock_resume_orphaned,
        ),
        patch("omniagentos.api.deps.get_store", lambda: object()),
        patch("omniagentos.api.routes.collab.get_collab_store", lambda: object()),
    ):
        from omniagentos.api.main import lifespan

        async def drive_lifespan():
            async with lifespan(app):
                pass

        asyncio.run(drive_lifespan())
        # Deterministic completion signal from inside the daemon thread —
        # never a wall-clock sleep racing thread scheduling.
        assert done.wait(timeout=30), "orch-resume daemon never invoked the resolver"

    # Verify resume_orphaned_orchestrations was called
    assert call_count == 1
    assert "store" in call_args
    assert "collab_store" in call_args
    assert call_args["limit"] == 8


def test_lifespan_resume_disabled_skips_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test lifespan hook skips resume when OMNIAGENTOS_ORCH_RESUME_ON_STARTUP=0."""
    monkeypatch.setenv("OMNIAGENTOS_ORCH_RESUME_ON_STARTUP", "0")
    monkeypatch.setenv("OMNIAGENTOS_SWARM_RESUME_ON_STARTUP", "0")

    call_count = 0

    def mock_resume_orphaned(*, store: object, collab_store: object, limit: int = 8):
        nonlocal call_count
        call_count += 1
        return []

    # Patch and test the lifespan
    with patch(
        "omniagentos.api.main._resolve_resume_orphaned_orchestrations",
        lambda: mock_resume_orphaned,
    ):
        import asyncio

        from omniagentos.api.main import lifespan

        async def test_lifespan():
            async with lifespan(app):
                pass

        asyncio.run(test_lifespan())

    # Verify resume_orphaned_orchestrations was NOT called
    assert call_count == 0


def test_lifespan_runs_swarm_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from omniagentos.api.routes import swarm as swarm_routes

    monkeypatch.setenv("OMNIAGENTOS_ORCH_RESUME_ON_STARTUP", "0")
    monkeypatch.setenv("OMNIAGENTOS_SWARM_RESUME_ON_STARTUP", "1")
    api_db = str(tmp_path / "campaign.db")
    monkeypatch.setattr(
        swarm_routes, "get_swarm_dal", lambda: type("Dal", (), {"db_path": api_db})()
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        scheduler_module,
        "resume_stale_swarms",
        lambda **kwargs: calls.append(kwargs) or {"resumed": [], "errors": []},
    )

    from omniagentos.api.main import lifespan

    async def drive_lifespan() -> None:
        async with lifespan(app):
            pass

    asyncio.run(drive_lifespan())
    assert calls == [{"db_path": api_db}]
