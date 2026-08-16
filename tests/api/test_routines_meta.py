"""LOOPS-1 API: scope/purpose serialization, next_run, engine heartbeat."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.db.store import SqliteStore
from omniagentos.scheduler.routines import compute_next_run
from tests.routines.conftest import (
    apply_routines_meta_migration,
    draft_routine_payload,
    valid_routine_payload,
)
from tests.support.db_template import make_store


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    store = make_store(SqliteStore, tmp_path / "routines_meta_api.db")
    apply_routines_meta_migration(store)
    return store


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_create_rejects_bogus_scope(database: SqliteStore) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/routines",
                    json=valid_routine_payload(scope="bogus", status="disabled"),
                )
                assert resp.status_code == 400
                body = resp.json()
                detail = body.get("error", {}).get("detail") or body
                flat = str(detail)
                assert "scope" in flat
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_create_omitting_meta_null_round_trips(database: SqliteStore) -> None:
    """LOOPS1-E2: omit scope/purpose → 2xx and NULL round-trips."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                # Disabled draft avoids D5 gate execution for this meta-only test.
                payload = draft_routine_payload(name="no-meta")
                created = await client.post("/api/routines", json=payload)
                assert created.status_code == 201, created.text
                body = created.json()
                assert body.get("scope") is None
                assert body.get("purpose") is None
                fetched = await client.get(f"/api/routines/{body['id']}")
                assert fetched.status_code == 200
                assert fetched.json().get("scope") is None
                assert fetched.json().get("purpose") is None
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_draft_post_omitting_engine_fields_persists_disabled(database: SqliteStore) -> None:
    """LOOPS1-E2 draft leg: disabled omit engine → 201, stays disabled."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = {"name": "composer-draft", "status": "disabled"}
                resp = await client.post("/api/routines", json=payload)
                assert resp.status_code == 201, resp.text
                body = resp.json()
                assert body["status"] == "disabled"
                assert body["name"] == "composer-draft"
                # Same payload with active (or default) → 400 naming missing fields.
                active_payload = {"name": "composer-active-attempt"}
                bad = await client.post("/api/routines", json=active_payload)
                assert bad.status_code == 400
                err = str(bad.json())
                assert "task_template" in err or "gate" in err or "validation" in err
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_next_run_on_list_and_get_matches_helper(database: SqliteStore) -> None:
    """LOOPS1-E3: API next_run equals compute_next_run over the same matcher."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = valid_routine_payload(
                    name="cron-next",
                    status="disabled",
                    trigger_type="cron",
                    trigger_config={"cron": "0 3 * * *"},
                )
                created = (await client.post("/api/routines", json=payload)).json()
                fetched = (await client.get(f"/api/routines/{created['id']}")).json()
                expected = compute_next_run(fetched, now=datetime.now(UTC))
                # Recompute with the same fields the helper sees.
                assert fetched["next_run"] == compute_next_run(
                    {
                        "trigger_type": fetched["trigger_type"],
                        "trigger_config": fetched["trigger_config"],
                        "last_fired": fetched.get("last_fired"),
                    },
                    now=datetime.now(UTC),
                )
                assert fetched["next_run"] == expected or fetched["next_run"] is not None

                event_payload = valid_routine_payload(
                    name="event-next",
                    status="disabled",
                    trigger_type="event",
                    trigger_config={"event": "run.completed"},
                )
                event = (await client.post("/api/routines", json=event_payload)).json()
                event_fetched = (await client.get(f"/api/routines/{event['id']}")).json()
                assert event_fetched["next_run"] is None
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_list_and_get_next_run_byte_identical(database: SqliteStore) -> None:
    """List and GET must return the same next_run bytes for the same row."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = valid_routine_payload(
                    name="cron-identical",
                    status="disabled",
                    trigger_type="cron",
                    trigger_config={"cron": "0 0 1 1 *"},
                )
                created = (await client.post("/api/routines", json=payload)).json()
                listed = (await client.get("/api/routines")).json()
                by_id = {r["id"]: r for r in listed}
                got = (await client.get(f"/api/routines/{created['id']}")).json()
                assert by_id[created["id"]]["next_run"] == got["next_run"]
                # Annual cron must resolve (not null) under the 400-day bound.
                assert got["next_run"] is not None
                assert got["next_run"].endswith("Z")
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_engine_heartbeat_null_when_absent(database: SqliteStore) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/routines/engine")
                assert resp.status_code == 200
                assert resp.json() == {"last_tick_at": None}
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_engine_heartbeat_returns_latest_routines_tick(database: SqliteStore) -> None:
    """LOOPS1-E3: synthetic routines.tick row is read back (never mocked at route)."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            ts = "2026-07-30T12:34:56Z"
            # Insert via the store's own event writer — same DB the route reads.
            database.insert_event(
                "routines.tick",
                "scheduler",
                "tick",
                target_type="scheduler",
                target_id="routines",
                payload={"n": 1},
            )
            # Overwrite ts to a known value for a stable assertion.
            with database._lock:
                database._connection.execute(
                    "UPDATE events SET ts = ? WHERE type = ?",
                    (ts, "routines.tick"),
                )
                database._connection.commit()

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/routines/engine")
                assert resp.status_code == 200
                assert resp.json()["last_tick_at"] == ts
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_scope_purpose_serialized_on_create(database: SqliteStore) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = draft_routine_payload(
                    name="scoped-draft",
                    scope="company",
                    purpose="goal_review",
                )
                resp = await client.post("/api/routines", json=payload)
                assert resp.status_code == 201, resp.text
                body = resp.json()
                assert body["scope"] == "company"
                assert body["purpose"] == "goal_review"
        finally:
            app.dependency_overrides.clear()

    _run(request())
