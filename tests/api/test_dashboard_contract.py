"""Anti-drift contract test for the Steward dashboard.

Every path `dashboard/src/features/steward/api.ts` calls against the FastAPI app is
enumerated here, LITERALLY, kept in sync BY HAND with the comment block near the
bottom of that file ("LITERAL paths (kept in sync by hand with
tests/api/test_dashboard_contract.py)"). This is the anti-drift net from the H2
lesson: no fixture may mask a real route mismatch — every GET path below is
actually requested against the real app (TestClient/asgi transport) with an empty
SqliteStore-backed database, and every POST path is asserted to be a route the
FastAPI app actually mounts.

If you add/rename/remove a fetch call in api.ts, update this file in the same
change.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.db.store import SqliteStore
from omniagentos.sessions import token
from tests.support.db_template import make_store

# GET paths -> (path, expected status against a freshly-created, empty database).
# Most list endpoints are expected to resolve to 200 with an empty collection; the
# few "singular" endpoints that 404 when nothing exists yet are called out by name
# per the brief ("GET paths expect != 404 given empty stores ... 404-for-latest-when-
# none is acceptable, assert specific expected codes per path").
GET_PATHS: list[tuple[str, int]] = [
    ("/api/briefings/latest", 404),  # no briefings exist yet -> not_found
    ("/api/briefings", 200),
    ("/api/goals", 200),
    ("/api/goals/does-not-exist", 404),  # unknown goal id -> not_found
    ("/api/comms/messages", 200),
    ("/api/comms/sources", 200),
    ("/api/suggestions", 200),
    ("/api/alerts", 200),
    ("/api/alerts/count", 200),
    ("/api/voice/audio/missing-artifact", 404),  # unknown artifact id -> not found
    ("/api/voice/providers", 200),
    ("/api/team/board", 200),
    ("/api/team/tree", 200),
    ("/api/team/evidence/unattributed", 200),
    ("/api/team/tasks/does-not-exist/evidence", 200),
    ("/api/team/tasks/does-not-exist/events", 200),
    ("/api/team/scoreboard", 200),
    ("/api/team/diagnostics", 200),
    # An empty board still renders a report rather than 500ing — the 07:00
    # message on a dead-quiet day is still a message.
    ("/api/team/report/preview", 200),
]

# POST paths -- existence-only check against app.routes. These are exercised with
# real side effects (task/run creation, suggestion state machine, etc.) by
# tests/steward/test_suggest_routes.py, tests/alerts/test_api.py, and
# tests/briefing/test_routes.py; this file only guards against api.ts drifting to a
# path the FastAPI app no longer mounts.
POST_PATH_TEMPLATES: list[str] = [
    "/api/briefings/generate",
    "/api/briefings/{briefing_id}/ack",
    "/api/suggestions/{suggestion_id}/approve",
    "/api/suggestions/{suggestion_id}/dismiss",
    "/api/alerts/{alert_id}/ack",
    "/api/team/tasks/{task_id}/verify",
    "/api/team/tasks/{task_id}/unverify",
    "/api/team/tasks/{task_id}/evidence",
]


@pytest.fixture
def sqlite_store(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "dashboard_contract.db")


@pytest.fixture
def asgi_client(
    sqlite_store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> httpx.AsyncClient:
    app.dependency_overrides[get_store] = lambda: sqlite_store
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-Session-Token": token.load_or_create_token()},
    )
    try:
        yield client
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()


@pytest.mark.parametrize("path,expected_status", GET_PATHS)
def test_get_path_resolves_against_the_real_app(
    asgi_client: httpx.AsyncClient, path: str, expected_status: int
) -> None:
    response = asyncio.run(asgi_client.get(path))
    assert response.status_code == expected_status, (
        f"GET {path} -> {response.status_code} (expected {expected_status}): {response.text}"
    )


def _iter_terminal_routes(routes: Iterable[Any]) -> Iterable[Any]:
    """Recursively flatten FastAPI's route tree.

    Newer FastAPI wraps `app.include_router(...)` results in an internal
    `_IncludedRouter` whose real APIRoute objects live under `.original_router.routes`
    (rather than exposing `.path`/`.methods` directly on `app.routes`) -- walk down
    through both `.original_router` and plain `.routes` (e.g. Mount) until we reach
    objects that actually expose `.path`.
    """
    for route in routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            yield from _iter_terminal_routes(getattr(original_router, "routes", []))
            continue
        nested_routes = getattr(route, "routes", None)
        if nested_routes is not None and not hasattr(route, "path"):
            yield from _iter_terminal_routes(nested_routes)
            continue
        yield route


def test_post_paths_are_mounted_in_the_app() -> None:
    mounted: set[tuple[str, str]] = set()
    for route in _iter_terminal_routes(app.routes):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or []
        if path is None:
            continue
        for method in methods:
            mounted.add((path, method))

    for template in POST_PATH_TEMPLATES:
        assert (template, "POST") in mounted, f"POST {template} is not mounted by the FastAPI app"
