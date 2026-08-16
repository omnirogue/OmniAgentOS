"""The board LIST projection: no ``swarm_json`` envelope, flat swarm fields, LIMIT.

``CollabStore.list_board_tasks`` used to ``SELECT *``. ``board_tasks.swarm_json``
(migration 045) is unbounded per card — a live board carried 47.8 MB of it across
~1k cards (``pre_attempt_dirty_paths`` alone), 96% of a 51 MB
``GET /api/board`` response — while the kanban reads exactly two fields
out of it. These tests pin the projection: the envelope never rides a list, the
two consumed fields do, single-card reads still return it whole, and the
projection can never silently drift out of sync with the table.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore

# A realistic offender: what actually inflated production was a per-attempt
# ``pre_attempt_dirty_paths`` array. ~200 KB per card here — small enough to keep
# the test fast, large enough that "did the envelope ride along?" is unambiguous.
_FAT_PATHS = [f"src/generated/module_{i:05d}/file_{i:05d}.py" for i in range(4000)]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _fat_swarm_json(phase: str | None = None, *, integration: bool = False) -> str:
    envelope: dict[str, Any] = {
        "task_key": "wp-1",
        "acceptance": "the board loads",
        "verify_command": "pytest -q",
        "pre_attempt_dirty_paths": _FAT_PATHS,
        "integration": integration,
    }
    if phase is not None:
        envelope["swarm_phase"] = phase
    return json.dumps(envelope)


def _set_swarm_json(store: CollabStore, task_id: str, raw: str) -> None:
    """Write ``swarm_json`` directly: it is not a PATCH-able column (the swarm
    scheduler owns it through its own DAL)."""
    conn = store._connection
    conn.execute("UPDATE board_tasks SET swarm_json = ? WHERE id = ?", (raw, task_id))
    conn.commit()


@pytest.fixture
def seeded(collab_store: CollabStore) -> dict[str, str]:
    """Three cards, deterministic ordering (newest first): fat swarm card in
    review, an integration card, a plain card with no swarm metadata."""
    ids: dict[str, str] = {}
    for key, title, created_at in (
        ("plain", "Plain intake card", "2026-07-31T00:00:01Z"),
        ("integration", "Integration card", "2026-07-31T00:00:02Z"),
        ("review", "Swarm card in review", "2026-07-31T00:00:03Z"),
    ):
        card = BoardTask(title=title, created_at=created_at, updated_at=created_at)
        collab_store.create_board_task(card)
        ids[key] = card.id
    _set_swarm_json(collab_store, ids["review"], _fat_swarm_json("review"))
    _set_swarm_json(collab_store, ids["integration"], _fat_swarm_json(integration=True))
    return ids


class TestProjectionDropsTheEnvelope:
    def test_list_carries_no_swarm_json(
        self, collab_store: CollabStore, seeded: dict[str, str]
    ) -> None:
        tasks = collab_store.list_board_tasks()
        assert len(tasks) == 3
        for task in tasks:
            assert "swarm_json" not in task
        # The whole list must now be cheap: the fat envelopes alone are ~400 KB.
        assert len(json.dumps(tasks)) < 20_000

    def test_list_carries_the_flattened_swarm_fields(
        self, collab_store: CollabStore, seeded: dict[str, str]
    ) -> None:
        by_id = {task["id"]: task for task in collab_store.list_board_tasks()}
        assert by_id[seeded["review"]]["swarm_phase"] == "review"
        assert by_id[seeded["review"]]["swarm_integration"] is False
        assert by_id[seeded["integration"]]["swarm_integration"] is True
        # No envelope at all -> phase absent, flag a real False (never None/1/0).
        assert by_id[seeded["plain"]]["swarm_phase"] is None
        assert by_id[seeded["plain"]]["swarm_integration"] is False

    def test_list_keeps_every_field_callers_read(
        self, collab_store: CollabStore, seeded: dict[str, str]
    ) -> None:
        """Python consumers read result_ref/run_id/archived_at; the UI reads the
        rest. Projection must not have quietly dropped any of them."""
        task = collab_store.list_board_tasks()[0]
        for field in (
            "id",
            "title",
            "description",
            "required_expertise",
            "status",
            "priority",
            "result_ref",
            "run_id",
            "archived_at",
            "archived",
            "org",
            "lane",
            "swarm_run_id",
            "created_at",
            "updated_at",
        ):
            assert field in task, field

    def test_open_tasks_for_is_projected_too(
        self, collab_store: CollabStore, seeded: dict[str, str]
    ) -> None:
        tasks = collab_store.open_tasks_for([])
        assert len(tasks) == 3
        assert all("swarm_json" not in task for task in tasks)
        assert all("swarm_phase" in task for task in tasks)

    def test_single_card_read_still_returns_the_envelope(
        self, collab_store: CollabStore, seeded: dict[str, str]
    ) -> None:
        """The projection is a LIST discipline, not a data loss: whoever needs
        the full envelope asks for one card."""
        row = collab_store.get_board_task(seeded["review"])
        assert row is not None
        assert json.loads(row["swarm_json"])["pre_attempt_dirty_paths"] == _FAT_PATHS
        # Single-card reads are unprojected, so they carry no flattened stand-ins.
        assert "swarm_integration" not in row

    def test_a_wrongly_typed_integration_flag_is_not_truthy(
        self, collab_store: CollabStore, seeded: dict[str, str]
    ) -> None:
        """Only json_extract's true representation counts. A string ``"false"``
        is TRUTHY in Python, so a ``bool()`` normalization would move the card
        into the Integration column; the envelope fallback it replaces was
        strict (``integration === true``), and so is this."""
        for raw_flag in ('"false"', '"true"', '"yes"', "0", "null", "[]", "{}"):
            _set_swarm_json(collab_store, seeded["plain"], f'{{"integration": {raw_flag}}}')
            card = next(t for t in collab_store.list_board_tasks() if t["id"] == seeded["plain"])
            assert card["swarm_integration"] is False, raw_flag
        # ...and a real JSON true still projects True.
        _set_swarm_json(collab_store, seeded["plain"], '{"integration": true}')
        card = next(t for t in collab_store.list_board_tasks() if t["id"] == seeded["plain"])
        assert card["swarm_integration"] is True

    def test_a_malformed_envelope_never_takes_the_board_down(
        self, collab_store: CollabStore, seeded: dict[str, str]
    ) -> None:
        """Bare ``json_extract`` on malformed JSON raises and would 500 the whole
        board over ONE bad row. The projection's ``json_valid`` guard degrades
        that card to 'no swarm metadata' and serves every other card."""
        _set_swarm_json(collab_store, seeded["plain"], "}{ not json at all")
        by_id = {task["id"]: task for task in collab_store.list_board_tasks()}
        assert len(by_id) == 3
        assert by_id[seeded["plain"]]["swarm_phase"] is None
        assert by_id[seeded["plain"]]["swarm_integration"] is False
        # The healthy cards are unaffected.
        assert by_id[seeded["review"]]["swarm_phase"] == "review"
        assert collab_store.open_tasks_for([]) != []


class TestProjectionCannotDrift:
    def test_projection_covers_every_board_tasks_column(self, collab_store: CollabStore) -> None:
        """A future migration that adds a column must add it to the projection
        (or to the omitted set, deliberately) — otherwise the board silently
        stops serving that field."""
        # Imported in-function so the rest of this module stays runnable against
        # a tree without the projection (that is what makes it a red-first test).
        from omniagentos.collab.store import _BOARD_LIST_COLUMNS, _BOARD_LIST_OMITTED

        actual = {
            str(row["name"])
            for row in collab_store._connection.execute("PRAGMA table_info(board_tasks)")
        }
        assert actual == set(_BOARD_LIST_COLUMNS) | set(_BOARD_LIST_OMITTED)


class TestLimit:
    def test_default_returns_every_row(
        self, collab_store: CollabStore, seeded: dict[str, str]
    ) -> None:
        assert len(collab_store.list_board_tasks()) == 3
        assert len(collab_store.list_board_tasks(limit=None)) == 3

    def test_limit_takes_the_newest_rows(
        self, collab_store: CollabStore, seeded: dict[str, str]
    ) -> None:
        tasks = collab_store.list_board_tasks(limit=2)
        assert [task["id"] for task in tasks] == [seeded["review"], seeded["integration"]]

    def test_limit_zero_returns_nothing(
        self, collab_store: CollabStore, seeded: dict[str, str]
    ) -> None:
        assert collab_store.list_board_tasks(limit=0) == []

    def test_limit_composes_with_status_and_archive_filters(
        self, collab_store: CollabStore, seeded: dict[str, str]
    ) -> None:
        collab_store.update_board_task(seeded["review"], {"status": BoardTaskStatus.DONE.value})
        assert len(collab_store.list_board_tasks(status="open", limit=1)) == 1
        assert [t["id"] for t in collab_store.list_board_tasks(status="done")] == [seeded["review"]]
        collab_store.update_board_task(seeded["plain"], {"archived_at": "2026-07-31T00:01:00Z"})
        assert [t["id"] for t in collab_store.list_board_tasks(archived=1, limit=5)] == [
            seeded["plain"]
        ]

    def test_negative_limit_is_refused(self, collab_store: CollabStore) -> None:
        """SQLite treats a negative LIMIT as 'no limit' — refuse rather than
        silently serve the whole board to a caller asking for a page."""
        with pytest.raises(ValueError):
            collab_store.list_board_tasks(limit=-1)


class TestBoardRoute:
    """The HTTP half of the contract, now on the ONE surviving board list.

    ``GET /api/collab/board`` — a second, unauthenticated listing of the same
    table, which no screen called — was removed; ``GET /api/board`` reads the
    same projection and reconciles each card against its linked run.
    """

    def test_route_response_is_projected(
        self, asgi_client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        resp = _run(asgi_client.get("/api/board"))
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 3
        assert all("swarm_json" not in card for card in body)
        by_id = {card["id"]: card for card in body}
        assert by_id[seeded["review"]]["swarm_phase"] == "review"
        assert by_id[seeded["integration"]]["swarm_integration"] is True
        # 3 cards with ~200 KB envelopes each used to mean a ~600 KB response.
        assert len(resp.content) < 20_000

    def test_route_limit_caps_the_page(
        self, asgi_client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        resp = _run(asgi_client.get("/api/board", params={"limit": 2}))
        assert resp.status_code == 200
        assert [card["id"] for card in resp.json()] == [
            seeded["review"],
            seeded["integration"],
        ]

    def test_route_without_limit_is_unchanged(
        self, asgi_client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        assert len(_run(asgi_client.get("/api/board")).json()) == 3

    def test_route_rejects_a_zero_or_negative_limit(
        self, asgi_client: httpx.AsyncClient, seeded: dict[str, str]
    ) -> None:
        assert _run(asgi_client.get("/api/board", params={"limit": 0})).status_code == 422
        assert _run(asgi_client.get("/api/board", params={"limit": -3})).status_code == 422
