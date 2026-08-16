"""Tests for board company enrichment and project_id preservation.

Covers: migration 087 project_id column preservation (not clobbering),
company enrichment from project → org_company_id → org_companies (slug),
and client-side company filtering — over ``GET /api/board``, the one board
list (its ``GET /api/collab/board`` twin, which these tests used to drive, was
a duplicate of the same table and has been removed).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _card(collab_store: CollabStore, title: str, **fields: Any) -> str:
    card = BoardTask(title=title, description=title)
    collab_store.create_board_task(card)
    if fields:
        collab_store.update_board_task(card.id, fields)
    return card.id


@pytest.fixture
def populated_orgs(store: SqliteStore) -> dict[str, str]:
    """Create test companies in org_companies."""
    now = utc_now_iso()
    # Create two test companies
    company_ids = {}
    for slug, name in [("acme", "ACME Corp"), ("widgets", "Widgets Inc")]:
        cid = f"co_{slug}"
        store._connection.execute(
            "INSERT INTO org_companies (id, slug, name, status, created_at) "
            "VALUES (?, ?, ?, 'active', ?)",
            (cid, slug, name, now),
        )
        company_ids[slug] = cid
    store._connection.commit()
    return company_ids


@pytest.fixture
def projects_with_companies(store: SqliteStore, populated_orgs: dict[str, str]) -> dict[str, str]:
    """Create projects linked to companies."""
    now = utc_now_iso()
    project_ids = {}
    # proj_acme → acme company
    proj_acme = "proj_acme"
    store._connection.execute(
        "INSERT INTO projects (id, name, created_at, org_company_id) "
        "VALUES (?, ?, ?, ?)",
        (proj_acme, "Project ACME", now, populated_orgs["acme"]),
    )
    project_ids["acme"] = proj_acme

    # proj_widgets → widgets company
    proj_widgets = "proj_widgets"
    store._connection.execute(
        "INSERT INTO projects (id, name, created_at, org_company_id) "
        "VALUES (?, ?, ?, ?)",
        (proj_widgets, "Project Widgets", now, populated_orgs["widgets"]),
    )
    project_ids["widgets"] = proj_widgets

    store._connection.commit()
    return project_ids


class TestProjectIdPreservation:
    """RED-FIRST: test that board_tasks.project_id column value is preserved."""

    def test_stored_project_id_not_clobbered_when_no_run_chain(
        self,
        asgi_client: httpx.AsyncClient,
        collab_store: CollabStore,
        projects_with_companies: dict[str, str],
    ) -> None:
        """A card with board_tasks.project_id set directly (no run chain)
        must keep it through the board read.

        The defect this pins was an unconditional
        ``task["project_id"] = project_map.get(...)`` on the (since removed)
        collab list route, which clobbered a stored value to None whenever the
        card had no run chain. ``GET /api/board`` never had that overwrite, and
        this asserts it never grows one.
        """
        # Create a card with project_id set directly (no run chain)
        card_id = _card(collab_store, "standalone", project_id=projects_with_companies["acme"])

        board = _run(asgi_client.get("/api/board")).json()
        card = next((t for t in board if t["id"] == card_id), None)

        # BUG: this will be None instead of proj_acme due to the unconditional overwrite
        assert card is not None, "Card should be on board"
        assert card["project_id"] == projects_with_companies["acme"], (
            f"Stored project_id must be preserved; got {card['project_id']}"
        )


class TestCompanyEnrichment:
    """Company enrichment from project → org_company_id → org_companies."""

    def test_card_with_project_gets_company_slug_enriched(
        self,
        asgi_client: httpx.AsyncClient,
        collab_store: CollabStore,
        projects_with_companies: dict[str, str],
    ) -> None:
        """A card linked to a project carrying org_company_id
        should be enriched with company_slug in org.organization_context."""
        card_id = _card(collab_store, "acme-card", project_id=projects_with_companies["acme"])

        board = _run(asgi_client.get("/api/board")).json()
        card = next((t for t in board if t["id"] == card_id), None)

        assert card is not None, "Card should be on board"
        assert card.get("org") is not None, "org envelope should exist"
        assert card["org"].get("organization_context") is not None
        assert card["org"]["organization_context"].get("company_slug") == "acme", (
            f"Company slug should be 'acme', got {card['org']['organization_context'].get('company_slug')}"
        )
        assert card["org"]["organization_context"].get("company_id") is not None

    def test_card_without_project_stays_unstamped(
        self,
        asgi_client: httpx.AsyncClient,
        collab_store: CollabStore,
    ) -> None:
        """A card with no project should have no company enrichment (honesty rule)."""
        card_id = _card(collab_store, "unscoped-card")

        board = _run(asgi_client.get("/api/board")).json()
        card = next((t for t in board if t["id"] == card_id), None)

        assert card is not None
        # org envelope may exist but company_slug should not be set
        if card.get("org") and card["org"].get("organization_context"):
            # Should not have company_slug if there's no project
            assert card["org"]["organization_context"].get("company_slug") is None or \
                   card["org"]["organization_context"].get("company_slug") == ""

    def test_classifier_set_company_not_overwritten(
        self,
        asgi_client: httpx.AsyncClient,
        collab_store: CollabStore,
        projects_with_companies: dict[str, str],
    ) -> None:
        """Never overwrite an existing classifier-set company_slug (honesty rule)."""
        import json

        # Create a card with a project pointing to "acme"
        # but with org_json already containing a different company from classifier
        card_id = _card(collab_store, "hybrid-card", project_id=projects_with_companies["acme"])

        # Manually set org_json with a classifier-provided company value
        org_data = {
            "organization_context": {
                "company_slug": "widgets",  # Classifier said widgets
                "company_id": "co_widgets"
            }
        }
        collab_store._store._write(
            "UPDATE board_tasks SET org_json = ? WHERE id = ?",
            (json.dumps(org_data), card_id)
        )

        board = _run(asgi_client.get("/api/board")).json()
        card = next((t for t in board if t["id"] == card_id), None)

        assert card is not None
        # The classifier value should be preserved, not overwritten by project
        assert card["org"]["organization_context"]["company_slug"] == "widgets", (
            "Classifier-set company should not be overwritten by project enrichment"
        )


class TestLiveBoardEnrichment:
    """Test company enrichment through the live board path (/api/board → reconcile_board)."""

    def test_live_board_enriches_company_from_project(
        self,
        asgi_client: httpx.AsyncClient,
        store,  # H1 store from fixture
        collab_store: CollabStore,
        projects_with_companies: dict[str, str],
    ) -> None:
        """The live board (GET /api/board via reconcile_board) enriches company
        from project→org_company_id→org_companies (shared helper).
        This is the real kanban path, not the collab API path.
        """
        # Create a card with a project
        card_id = _card(collab_store, "live-board-test", project_id=projects_with_companies["acme"])

        # GET /api/board (intake.py:1454 live_board → reconcile_board)
        board = _run(asgi_client.get("/api/board")).json()
        card = next((t for t in board if t["id"] == card_id), None)

        assert card is not None, "Card should appear on live board"
        assert card.get("org") is not None, "org envelope should exist"
        assert card["org"].get("organization_context") is not None
        assert card["org"]["organization_context"].get("company_slug") == "acme", (
            f"Company should be enriched via reconcile_board: got {card['org']['organization_context'].get('company_slug')}"
        )
        assert card["org"]["organization_context"].get("company_id") is not None


class TestAtomicity:
    """Test the both-or-neither atomicity guard for company enrichment."""

    def test_classifier_slug_only_not_overwritten_with_project_id(
        self,
        asgi_client: httpx.AsyncClient,
        collab_store: CollabStore,
        projects_with_companies: dict[str, str],
    ) -> None:
        """A card with org_json carrying ONLY company_slug (from classifier)
        and a project pointing to a DIFFERENT company should preserve the
        classifier value and NOT add company_id (honesty rule: both-or-neither).

        This tests the guard at enrich.py:88-91 which ensures we never set
        one of {company_slug, company_id} without the other.
        """
        import json

        # Create a card with project→acme
        card_id = _card(collab_store, "atomicity-test", project_id=projects_with_companies["acme"])

        # Manually set org_json with ONLY company_slug from classifier (no company_id)
        org_data = {
            "organization_context": {
                "company_slug": "widgets",  # Different from the project's acme
            }
        }
        collab_store._store._write(
            "UPDATE board_tasks SET org_json = ? WHERE id = ?",
            (json.dumps(org_data), card_id)
        )

        board = _run(asgi_client.get("/api/board")).json()
        card = next((t for t in board if t["id"] == card_id), None)

        assert card is not None
        # After enrichment: classifier's company_slug should be preserved
        assert card["org"]["organization_context"]["company_slug"] == "widgets", (
            "Classifier-set company_slug should be preserved"
        )
        # company_id should remain absent (both-or-neither guard)
        company_id = card["org"]["organization_context"].get("company_id")
        assert company_id is None or company_id == "", (
            f"company_id should be absent when only company_slug is set (atomicity guard); got {company_id}"
        )
