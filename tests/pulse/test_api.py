"""HTTP surface: GET /api/pulse/series, GET /api/system/delta.

Uses the same ASGI-against-httpx pattern the other route tests use so the
dependency override reaches the in-memory SqliteStore.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.db.store import SqliteStore
from omniagentos.pulse.store import PulseStore
from tests.pulse.conftest import (
    seed_board_tasks,
    seed_chats,
    seed_improvements,
    seed_routine_runs,
    seed_skills,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_series_unknown_metric_is_422(database: SqliteStore) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/pulse/series?metric=nope.nope&days=30")
                assert resp.status_code == 422
                body = resp.json()
                assert "unknown metric" in body["error"]["message"]
                assert "skills.total" in body["error"]["detail"]["known"]
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_series_returns_points_ordered(database: SqliteStore) -> None:
    today = datetime.now(UTC).date()
    dates = [
        (today - timedelta(days=2)).isoformat(),
        (today - timedelta(days=1)).isoformat(),
        today.isoformat(),
    ]
    PulseStore(database).upsert_many([
        ("skills.total", dates[0], 1.0),
        ("skills.total", dates[1], 2.0),
        ("skills.total", dates[2], 3.0),
    ])

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/pulse/series?metric=skills.total&days=30")
                assert resp.status_code == 200
                body = resp.json()
                assert body["metric"] == "skills.total"
                assert len(body["points"]) == 3
                # Oldest first (pinned contract).
                assert body["points"][0]["date"] == dates[0]
                assert body["points"][-1]["date"] == dates[2]
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_series_seeds_on_empty(database: SqliteStore) -> None:
    """First request against an empty pulse_series runs the aggregator inline."""
    seed_skills(database, n=2)

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/pulse/series?metric=skills.total&days=30")
                assert resp.status_code == 200
                body = resp.json()
                # Seeding populates today's row: exactly 1 point, value = 2.
                assert len(body["points"]) == 1
                assert body["points"][0]["value"] == 2.0
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_series_seed_does_not_counterfeit_zero_versions_on_bad_timestamps(
    database: SqliteStore,
) -> None:
    """Unreadable skill_versions must not seed as empty→0 while total is ready.

    The production GET handler discards snapshot()'s nullable map and returns
    series() only. A soft-unknown versions metric becomes points=[] which the
    mixed Skills tile renders as "0 versions this week". Fail-closed seed
    leaves *both* series empty instead of a mixed ready/zero tile.
    """
    seed_skills(database, n=1)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for version, created_at in enumerate(
        (now, "2026-02-30T00:00:00Z", "not-a-date"),
        start=1,
    ):
        database._connection.execute(
            "INSERT INTO skill_versions "
            "(id, skill_id, version, content_snapshot, change_reason, author, "
            "status, created_at) "
            "VALUES (?, 'sk_0', ?, '', '', 'test', 'active', ?)",
            (f"skv_{version}", version, created_at),
        )
    database._connection.commit()

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                versions = await client.get(
                    "/api/pulse/series?metric=skills.versions&days=30"
                )
                total = await client.get(
                    "/api/pulse/series?metric=skills.total&days=30"
                )
                assert versions.status_code == 200
                assert total.status_code == 200
                # Per-metric error: bad skill_versions timestamp fails that metric only
                # versions has no point (failed metric), but total has a point (succeeded)
                assert versions.json()["points"] == []
                assert "error" in versions.json()  # error field shows failure reason
                # skills.total still succeeds and is seeded
                assert total.json()["points"] != []
                assert total.json()["points"][0]["value"] == 1.0
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_list_metrics(database: SqliteStore) -> None:
    PulseStore(database).upsert_many([
        ("skills.total", "2026-01-01", 1.0),
        ("loops.fires", "2026-01-01", 0.0),
    ])

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/pulse/metrics")
                assert resp.status_code == 200
                assert set(resp.json()["metrics"]) == {"skills.total", "loops.fires"}
        finally:
            app.dependency_overrides.clear()

    _run(request())


# ── /api/system/delta ──────────────────────────────────────────────────────


def test_delta_requires_since_param(database: SqliteStore, auth_headers: dict[str, str]) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                # The route is session-token-gated like every /api/system GET;
                # an authenticated caller missing `since` gets FastAPI's 422.
                resp = await client.get("/api/system/delta", headers=auth_headers)
                assert resp.status_code == 422
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_delta_returns_pinned_shape_seeded(
    database: SqliteStore, auth_headers: dict[str, str]
) -> None:
    seed_skills(database, n=1)
    seed_improvements(database, statuses=["applied"])
    seed_routine_runs(database, today_count=2, accepted=1)
    seed_board_tasks(database, done=3, archived=1)
    seed_chats(database, active=2)

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            # use a timestamp long ago to capture all seeded rows
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/api/system/delta?since=2020-01-01T00:00:00Z", headers=auth_headers
                )
                assert resp.status_code == 200
                body = resp.json()
                # Required keys (pinned contract).
                for key in (
                    "since",
                    "skills_updated",
                    "improvements_decided",
                    "loops_run",
                    "tasks_completed",
                    "chats_active",
                ):
                    assert key in body, f"missing key {key}"
                # Seeded data must show up.
                assert body["skills_updated"] >= 1
                assert body["improvements_decided"] >= 1
                assert body["loops_run"] == 2
                assert body["tasks_completed"] == 4  # 3 done + 1 archived
                assert body["chats_active"] == 2
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_delta_caps_server_side_at_30_days(
    database: SqliteStore, auth_headers: dict[str, str]
) -> None:
    """Asking for older than 30 days must not return events older than 30 days."""
    # Insert an improvement with an updated_at well outside the 30-day cap.
    long_ago = "2020-01-01T00:00:00Z"
    database._connection.execute(
        "INSERT INTO improvements (id, origin, kind, title, status, risk_level, "
        "created_at, updated_at) VALUES ('imp_old', 'realtime', 'fix', 'Old', "
        "'applied', 2, ?, ?)",
        (long_ago, long_ago),
    )
    database._connection.commit()

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(
                    "/api/system/delta?since=2020-01-01T00:00:00Z", headers=auth_headers
                )
                body = resp.json()
                # The 30-day cap clamps `since` server-side; the old improvement
                # (updated_at = 2020) cannot be within 30 days of now.
                assert body["improvements_decided"] == 0
                # The response's `since` must be recent (within 30 days of now).
                resp_since = datetime.fromisoformat(
                    body["since"].replace("Z", "+00:00")
                )
                assert (datetime.now(UTC) - resp_since).days <= 31
        finally:
            app.dependency_overrides.clear()

    _run(request())
