"""Lane B decisive: the review queue reports what the production tick staged.

Governing rule
--------------
Drive ``routines_tick.tick`` (the live launchd entry) and observe through
``GET /api/memlife/queue`` — the real HTTP handler. This file must **not**
INSERT a candidate row, must **not** import ``run_dream_cycle``, and must
**not** import ``memlife.db``. Every row observed must have arrived through
``tick()``.

Named counterfeits (each must make the decisive test RED)
---------------------------------------------------------
- ``counterfeit_queue_counts_the_filesystem`` — glob candidates/*.json;
  graduate still 404s. The graduate step is not optional.
- ``counterfeit_insert_on_read`` — get_queue lazily inserts; catch with
  ``test_rows_exist_before_any_http_request``.
- ``counterfeit_upsert_resets_status`` — ON CONFLICT DO UPDATE status=staged;
  killed by reject-then-retick in test_dream_db_sync.
- ``counterfeit_zero_for_unknown_salience`` — salience or 0.0; killed by
  IS NULL assertion in test_dream_db_sync.
- ``counterfeit_fs_only_on_db_failure`` — swallow stage_candidate errors;
  killed by failure-status + no FS file assertion in test_dream_db_sync.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.db.store import SqliteStore
from omniagentos.memlife.contracts import EpisodicEvent, EventResult
from omniagentos.memlife.dream import ensure_dream_cycle_routine
from omniagentos.memlife.store import MemlifeStore
from omniagentos.policy import load_policy
from omniagentos.scheduler.routines_tick import tick
from tests.support.db_template import make_store

DUE_NOW = datetime(2026, 7, 29, 3, 0, tzinfo=UTC)
ENV_STORE = "OMNIAGENTOS_MEMLIFE_STORE"
EVENT_TS = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _valid_event_line(event_id: str, *, reflection: str) -> str:
    event = EpisodicEvent(
        id=event_id,
        ts=EVENT_TS,
        skill="swarm.coder",
        action="attempt",
        result=EventResult.FAILURE,
        pain=8.0,
        importance=9.0,
        reflection=reflection,
    )
    return event.model_dump_json()


def _seed_two_events(root: Path) -> None:
    """Two claims that do not Jaccard-cluster → staged=2."""
    events_path = root / "episodic" / "events.jsonl"
    lines = [
        _valid_event_line(
            "ev_a",
            reflection="Agents cannot commit inside a sandboxed worktree",
        ),
        _valid_event_line(
            "ev_b",
            reflection="Always pin exact dependency versions in lockfiles",
        ),
    ]
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "queue_reflects.db")


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


# ---------------------------------------------------------------------------
# Decisive: tick → queue → graduate → queue
# ---------------------------------------------------------------------------


def test_queue_endpoint_reports_what_the_tick_staged(
    database: SqliteStore,
    memlife_root: Path,
    client: httpx.AsyncClient,
) -> None:
    """Production tick fills the SQLite queue the API counts; graduate works."""
    ensure_dream_cycle_routine(database)
    _seed_two_events(memlife_root)

    result = tick(database, load_policy(), now=DUE_NOW)
    assert len(result["fired"]) == 1, result
    assert result["fired"][0]["fired"] is True

    # Prefer filesystem queue snapshot — no route surface change, proves both
    # stores agree (spec B4).
    queue_snap = MemlifeStore(memlife_root).load_queue()
    ids = list(queue_snap["pending"])
    assert len(ids) == 2, queue_snap

    response = _run(client.get("/api/memlife/queue"))
    assert response.status_code == 200, response.text
    assert response.json() == {"pending": 2}

    one_id = ids[0]
    graduated = _run(
        client.post(
            f"/api/memlife/{one_id}/graduate",
            json={"decided_by": "owner", "reason": "reproducible"},
        )
    )
    assert graduated.status_code == 200, graduated.text
    assert graduated.json()["status"] == "graduated"

    after = _run(client.get("/api/memlife/queue"))
    assert after.status_code == 200, after.text
    assert after.json() == {"pending": 1}


def test_rows_exist_before_any_http_request(
    database: SqliteStore,
    memlife_root: Path,
) -> None:
    """Counterfeit kill for insert-on-read: rows must exist after tick alone."""
    ensure_dream_cycle_routine(database)
    _seed_two_events(memlife_root)

    tick(database, load_policy(), now=DUE_NOW)

    # Query SQLite directly BEFORE creating any HTTP client.
    with database._lock:
        rows = database._connection.execute(
            "SELECT id, status FROM memlife_candidates ORDER BY id"
        ).fetchall()
    assert len(rows) == 2, rows
    assert all(r["status"] == "staged" for r in rows)
