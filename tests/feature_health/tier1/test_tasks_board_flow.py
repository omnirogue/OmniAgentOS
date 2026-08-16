"""Feature-health tier1 — full board-task HTTP lifecycle ($0, no LLM).

Drives the isolated app via dependency_overrides (idioms from
``tests/intake/test_api.py`` and ``tests/collab/conftest.py``):

POST /api/intake/dispatch with a readonly-class brief (mock harness; readonly
runs are deterministic — no planner/LLM is consulted) → GET /api/board →
claim (CAS on claim_version) → PATCH in_progress → PATCH done + result_ref →
archive (the intake route, which pauses linked work) → restore; and the guard
that ``update_board_task`` REFUSES a direct ``status → claimed`` write
(claim_task CAS is the only path).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore


@pytest.fixture()
def collab() -> CollabStore:
    return CollabStore(":memory:")


@pytest.fixture()
def client(collab: CollabStore) -> Iterator[httpx.AsyncClient]:
    store = collab._store  # share one in-memory DB across both store deps.
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_collab_store] = lambda: collab
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


def _get(coro: Any) -> Any:
    return asyncio.run(coro)


def _board_card(client: httpx.AsyncClient, card_id: str, archived: int = 0) -> dict[str, Any] | None:
    rows = _get(client.get("/api/board", params={"archived": archived})).json()
    return next((row for row in rows if row["id"] == card_id), None)


def _stored_card(collab: CollabStore, card_id: str) -> dict[str, Any]:
    """The card as the board store holds it.

    Claim state is asserted here rather than through ``GET /api/board``: the
    live board RECONCILES each card against its linked run, and this card's run
    is still queued, so the board legitimately renders it in the queued run's
    column. That projection is the live board's contract; ``claimed_by`` /
    ``claim_version`` are the CAS contract, and this reads them where they live.
    """
    row = collab.get_board_task(card_id)
    assert row is not None
    return row


def test_full_board_task_lifecycle(client: httpx.AsyncClient, collab: CollabStore) -> None:
    # 1) Dispatch a readonly-class brief — deterministic, no planner/LLM seam hit.
    dispatch = _get(
        client.post(
            "/api/intake/dispatch",
            json={
                "title": "FH board lifecycle probe",
                "description": "walk a card through the whole board",
                "acceptance_criteria": ["every transition observed over HTTP"],
                "suggested_priority": "normal",
                "harness": "mock",
                "execute": "readonly",
            },
        )
    )
    assert dispatch.status_code == 201, dispatch.text
    data = dispatch.json()
    assert data["execute"] == "readonly"
    assert data["task_id"].startswith("tsk")
    assert data["run_id"] is not None and data["run_id"].startswith("run")
    card_id = data["board_task"]["id"]
    assert data["board_task"]["status"] == "open"

    # 2) The card is on the live board, open, with its run linked.
    card = _board_card(client, card_id)
    assert card is not None
    assert card["status"] == "open"
    assert card["run_id"] == data["run_id"]
    assert int(card["claim_version"] or 0) == 0

    # 3) Claim via the CAS route (expect_version = current claim_version).
    agent = _get(client.post("/api/collab/agents", json={"name": "fh-agent"})).json()
    claim = _get(
        client.post(f"/api/collab/board/{card_id}/claim", json={"agent_id": agent["id"]})
    )
    assert claim.status_code == 200, claim.text
    assert claim.json() == {
        "success": True,
        "owner_employee_id": None,
        "claimed_by": agent["id"],
    }
    card = _stored_card(collab, card_id)
    assert card["status"] == "claimed"
    assert card["claimed_by"] == agent["id"]
    assert int(card["claim_version"]) == 1  # CAS bumped the version

    # 4) A second claim loses the CAS: 409 claim_conflict.
    rival = _get(client.post("/api/collab/agents", json={"name": "fh-rival"})).json()
    second = _get(
        client.post(f"/api/collab/board/{card_id}/claim", json={"agent_id": rival["id"]})
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "claim_conflict"
    assert _stored_card(collab, card_id)["claimed_by"] == agent["id"]

    # 5) Progress, then complete with a result_ref.
    progressed = _get(
        client.patch(f"/api/collab/board/{card_id}", json={"status": "in_progress"})
    )
    assert progressed.status_code == 200, progressed.text
    assert progressed.json()["status"] == "in_progress"

    done = _get(
        client.patch(
            f"/api/collab/board/{card_id}",
            json={"status": "done", "result_ref": "note:fh-lifecycle-proof"},
        )
    )
    assert done.status_code == 200, done.text
    row = done.json()
    assert row["status"] == "done"
    assert row["result_ref"] == "note:fh-lifecycle-proof"

    # 6) Archive through the ONE archive route (intake's: it pauses the card's
    #    linked run/session first). The collab twin, which only stamped
    #    archived_at and left the work running, has been removed.
    archived = _get(client.post(f"/api/board/{card_id}/archive"))
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] is not None
    assert _board_card(client, card_id) is None
    archived_row = _board_card(client, card_id, archived=1)
    assert archived_row is not None and archived_row["archived_at"] is not None

    # 7) Restore: back on the live board, archived_at cleared, result kept.
    restored = _get(client.post(f"/api/collab/board/{card_id}/restore"))
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived_at"] is None
    back = _board_card(client, card_id)
    assert back is not None
    assert back["archived_at"] is None
    assert back["result_ref"] == "note:fh-lifecycle-proof"
    # The live board projects each card's column from its LINKED RUN, and this
    # card's mock run never left the queue — so the reconciled column is the
    # queued run's, not the hand-PATCHed "done". Asserted, not hidden: it is the
    # difference between the board's projection and the card's stored status.
    assert back["status"] == "open"
    assert back["run_state"] == "queued"


def test_direct_status_to_claimed_write_is_refused(
    client: httpx.AsyncClient, collab: CollabStore
) -> None:
    """claim_task's CAS is the ONLY path to CLAIMED — PATCH and store both refuse."""
    card = BoardTask(title="FH claim-guard probe")
    collab.create_board_task(card)

    # HTTP surface: PATCH status=claimed → 400 validation.
    response = _get(
        client.patch(f"/api/collab/board/{card.id}", json={"status": "claimed"})
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "validation"
    assert "claim" in response.json()["error"]["message"].lower()

    # Store seam: update_board_task refuses the same write with ValueError.
    with pytest.raises(ValueError, match="CLAIMED"):
        collab.update_board_task(card.id, {"status": "claimed"})

    # Far side: the row never moved.
    row = collab.get_board_task(card.id)
    assert row["status"] == "open"
    assert row["claimed_by"] is None
