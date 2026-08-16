"""The generic board PATCH may not decide an automation proposal.

The the operator-only gate lives in ``/task approve`` | ``/task reject``
(``omniagentos.team.tasks``), but ``PATCH /api/collab/board/{id}`` derives ANY
authenticated principal and calls the generic mutation — so a gate enforced only
in the Slack handler is a gate with a door beside it. These tests drive the HTTP
surface an attacker (or an honest script) would actually use.

The refusal is at the STORE, which is why it covers this route without the route
knowing anything about proposals.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


@pytest.fixture
def proposal(collab_store: CollabStore) -> BoardTask:
    """An awaiting-approval automation proposal, as ``/task propose`` writes it."""
    card = BoardTask(
        title="draft the weekly digest",
        status=BoardTaskStatus.AWAITING_APPROVAL,
        source="automation-proposal",
        acceptance_criteria="draft the weekly digest",
    )
    collab_store.create_board_task(card)
    return card


@pytest.mark.parametrize("status", ["open", "cancelled", "done"])
def test_a_principal_cannot_patch_a_proposal_to_a_decision(
    asgi_client: httpx.AsyncClient, collab_store: CollabStore, proposal: BoardTask, status: str
) -> None:
    response = _run(
        asgi_client.patch(
            f"/api/collab/board/{proposal.id}",
            json={"status": status},
            headers={"X-Omni-Authenticated-Principal": "emp_alice"},
        )
    )
    assert response.status_code == 400, response.text
    assert "automation_proposal_decision_required" in response.text
    assert "/task approve" in response.text, "the refusal must name the way through"
    # Never trust the handler's own return — read the far side directly.
    row = collab_store.get_board_task(proposal.id)
    assert row is not None
    assert row["status"] == BoardTaskStatus.AWAITING_APPROVAL.value


def test_the_source_cannot_be_stripped_first(
    asgi_client: httpx.AsyncClient, collab_store: CollabStore, proposal: BoardTask
) -> None:
    """The two-step bypass: relabel the card, then move it. Step one fails."""
    response = _run(
        asgi_client.patch(
            f"/api/collab/board/{proposal.id}",
            json={"source": "decision"},
            headers={"X-Omni-Authenticated-Principal": "emp_alice"},
        )
    )
    assert response.status_code == 400, response.text
    assert "source_boundary_immutable" in response.text
    row = collab_store.get_board_task(proposal.id)
    assert row is not None
    assert row["source"] == "automation-proposal"


def test_an_ordinary_edit_to_a_proposal_still_works(
    asgi_client: httpx.AsyncClient, collab_store: CollabStore, proposal: BoardTask
) -> None:
    """The guard is narrow on purpose: only the DECISION is reserved. Fixing a
    typo in a proposal must not require the operator."""
    response = _run(
        asgi_client.patch(
            f"/api/collab/board/{proposal.id}",
            json={"description": "with a worked example"},
            headers={"X-Omni-Authenticated-Principal": "emp_bob"},
        )
    )
    assert response.status_code == 200, response.text
    row = collab_store.get_board_task(proposal.id)
    assert row is not None
    assert row["description"] == "with a worked example"
    assert row["status"] == BoardTaskStatus.AWAITING_APPROVAL.value


def test_an_ordinary_card_in_the_review_bucket_is_unaffected(
    asgi_client: httpx.AsyncClient, collab_store: CollabStore
) -> None:
    """``awaiting_approval`` is a shared bucket — a swarm card waiting on a
    human call must keep moving through the ordinary board API."""
    card = BoardTask(title="Swarm work", status=BoardTaskStatus.AWAITING_APPROVAL)
    collab_store.create_board_task(card)
    response = _run(
        asgi_client.patch(
            f"/api/collab/board/{card.id}",
            json={"status": "open"},
            headers={"X-Omni-Authenticated-Principal": "emp_alice"},
        )
    )
    assert response.status_code == 200, response.text
    row = collab_store.get_board_task(card.id)
    assert row is not None
    assert row["status"] == BoardTaskStatus.OPEN.value
