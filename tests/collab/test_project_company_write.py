"""RED-FIRST: the PATCH company writer must close the loop enrichment already opened.

``omniagentos/orgdims/enrich.py`` reads ``projects.org_company_id`` to stamp a
board card's ``organization_context.company_slug`` -- but until this lane there
was no writer for that column anywhere in the product. This proves the writer
end-to-end: PATCH /api/projects/{id} sets the column (verified via a DIRECT
database read-back, never the handler's own JSON return), and a board card
scoped to that project is enriched with the resulting company_slug through
GET /api/board -- the same path a real operator's board relies on.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _insert_company(store: SqliteStore, company_id: str, slug: str, name: str) -> None:
    store._connection.execute(  # noqa: SLF001
        "INSERT INTO org_companies (id, slug, name, status, created_at) "
        "VALUES (?, ?, ?, 'active', ?)",
        (company_id, slug, name, utc_now_iso()),
    )
    store._connection.commit()  # noqa: SLF001


def test_patch_project_company_closes_the_enrichment_loop(
    asgi_client: httpx.AsyncClient, store: SqliteStore, collab_store: CollabStore
) -> None:
    _insert_company(store, "co_acme", "acme", "ACME Corp")

    project = _run(asgi_client.post("/api/projects", json={"name": "Acme Delivery"})).json()

    # Operators think in slugs -- PATCH accepts the slug, not the raw id.
    patch_response = _run(
        asgi_client.patch(
            f"/api/projects/{project['id']}",
            json={"org_company_id": "acme"},
        )
    )
    assert patch_response.status_code == 200
    assert patch_response.json().get("org_company_id") == "co_acme"

    # Never trust the handler's own return -- read the far side directly.
    row = store._connection.execute(  # noqa: SLF001
        "SELECT org_company_id FROM projects WHERE id = ?", (project["id"],)
    ).fetchone()
    assert row is not None
    assert row["org_company_id"] == "co_acme"

    card = BoardTask(title="acme-card", description="acme-card")
    collab_store.create_board_task(card)
    collab_store.update_board_task(card.id, {"project_id": project["id"]})

    board = _run(asgi_client.get("/api/board")).json()
    board_card = next((t for t in board if t["id"] == card.id), None)
    assert board_card is not None, "card should be on the board"
    org = board_card.get("org") or {}
    context = org.get("organization_context") or {}
    assert context.get("company_slug") == "acme", (
        f"writer must close the loop enrichment already opened; got {context}"
    )
    assert context.get("company_id") == "co_acme"


def test_patch_project_company_unknown_returns_400_and_db_unchanged(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    _insert_company(store, "co_acme", "acme", "ACME Corp")
    project = _run(asgi_client.post("/api/projects", json={"name": "No Company"})).json()

    response = _run(
        asgi_client.patch(
            f"/api/projects/{project['id']}",
            json={"org_company_id": "not-a-real-company"},
        )
    )
    assert response.status_code == 400
    body = response.json()
    assert "acme" in str(body)

    row = store._connection.execute(  # noqa: SLF001
        "SELECT org_company_id FROM projects WHERE id = ?", (project["id"],)
    ).fetchone()
    assert row is not None
    assert row["org_company_id"] is None


def test_patch_project_company_null_clears_assignment(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    _insert_company(store, "co_acme", "acme", "ACME Corp")
    project = _run(asgi_client.post("/api/projects", json={"name": "Clear Me"})).json()

    set_response = _run(
        asgi_client.patch(
            f"/api/projects/{project['id']}",
            json={"org_company_id": "co_acme"},  # canonical id form also accepted
        )
    )
    assert set_response.status_code == 200

    clear_response = _run(
        asgi_client.patch(
            f"/api/projects/{project['id']}",
            json={"org_company_id": None},
        )
    )
    assert clear_response.status_code == 200
    assert clear_response.json().get("org_company_id") is None

    row = store._connection.execute(  # noqa: SLF001
        "SELECT org_company_id FROM projects WHERE id = ?", (project["id"],)
    ).fetchone()
    assert row is not None
    assert row["org_company_id"] is None


def test_patch_project_company_and_jira_conflict_rolls_back_both(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    """Transaction test: org_company_id set AND jira_project_key uniqueness fails
    -> BOTH roll back, nothing partially applied."""
    _insert_company(store, "co_acme", "acme", "ACME Corp")
    holder = _run(asgi_client.post("/api/projects", json={"name": "Key Holder"})).json()
    target = _run(asgi_client.post("/api/projects", json={"name": "Target"})).json()

    # holder claims ACM first.
    ok = _run(
        asgi_client.patch(f"/api/projects/{holder['id']}", json={"jira_project_key": "ACM"})
    )
    assert ok.status_code == 200

    conflict = _run(
        asgi_client.patch(
            f"/api/projects/{target['id']}",
            json={"org_company_id": "acme", "jira_project_key": "ACM"},
        )
    )
    assert conflict.status_code == 409

    row = store._connection.execute(  # noqa: SLF001
        "SELECT org_company_id, jira_project_key FROM projects WHERE id = ?",
        (target["id"],),
    ).fetchone()
    assert row is not None
    assert row["org_company_id"] is None
    assert row["jira_project_key"] is None


def test_patch_project_company_valid_jira_plus_unknown_company_leaves_jira_key_unapplied(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    """Sol ADOPT 4: the inverse rollback direction.

    The existing conflict test's jira check raises BEFORE any UPDATE runs
    (nothing was ever staged), so it never proves that a company failure can
    undo an ALREADY-APPLIED jira write in the same transaction. Here the jira
    key is valid and uncontested (no uniqueness clash) -- only the company
    resolution fails -- so the jira UPDATE genuinely executes (uncommitted)
    before the company step raises and the whole transaction rolls back.
    """
    project = _run(asgi_client.post("/api/projects", json={"name": "Both Fields"})).json()

    response = _run(
        asgi_client.patch(
            f"/api/projects/{project['id']}",
            json={"jira_project_key": "HOO", "org_company_id": "not-a-real-company"},
        )
    )
    assert response.status_code == 400

    row = store._connection.execute(  # noqa: SLF001
        "SELECT org_company_id, jira_project_key FROM projects WHERE id = ?",
        (project["id"],),
    ).fetchone()
    assert row is not None
    assert row["jira_project_key"] is None, (
        f"a valid jira key must NOT survive when the company step in the same "
        f"transaction fails; got {row['jira_project_key']!r}"
    )
    assert row["org_company_id"] is None


def test_patch_project_company_field_absent_leaves_assignment_untouched(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    """Sol MUST FIX 1: field absent from the PATCH body must never touch the column."""
    _insert_company(store, "co_acme", "acme", "ACME Corp")
    project = _run(asgi_client.post("/api/projects", json={"name": "Untouched"})).json()
    set_response = _run(
        asgi_client.patch(f"/api/projects/{project['id']}", json={"org_company_id": "acme"})
    )
    assert set_response.status_code == 200

    other = _run(asgi_client.post("/api/projects", json={"name": "Other Parent"})).json()
    # A PATCH that mentions parent_project_id but never org_company_id at all.
    reparent = _run(
        asgi_client.patch(
            f"/api/projects/{project['id']}",
            json={"parent_project_id": other["id"]},
        )
    )
    assert reparent.status_code == 200

    row = store._connection.execute(  # noqa: SLF001
        "SELECT org_company_id FROM projects WHERE id = ?", (project["id"],)
    ).fetchone()
    assert row is not None
    assert row["org_company_id"] == "co_acme", "absent field must leave the assignment untouched"


def test_patch_project_company_explicit_null_clears_a_prior_assignment(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    """Sol MUST FIX 1: explicit JSON null is the ONLY way to clear (contrast with blank)."""
    _insert_company(store, "co_acme", "acme", "ACME Corp")
    project = _run(asgi_client.post("/api/projects", json={"name": "Null Clears"})).json()
    set_response = _run(
        asgi_client.patch(f"/api/projects/{project['id']}", json={"org_company_id": "acme"})
    )
    assert set_response.status_code == 200

    clear_response = _run(
        asgi_client.patch(f"/api/projects/{project['id']}", json={"org_company_id": None})
    )
    assert clear_response.status_code == 200
    assert clear_response.json().get("org_company_id") is None

    row = store._connection.execute(  # noqa: SLF001
        "SELECT org_company_id FROM projects WHERE id = ?", (project["id"],)
    ).fetchone()
    assert row is not None
    assert row["org_company_id"] is None


def test_patch_project_company_blank_string_is_rejected_not_treated_as_clear(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    """Sol MUST FIX 1 (the exact bug caught in review): "" must 400, never NULL the column.

    Probes both a bare empty string and a whitespace-only string against a
    project that already carries a live company -- reproducing Sol's exact
    finding (DB readback went from co_active to NULL under the old code).
    """
    _insert_company(store, "co_active", "active-co", "Active Co")
    project = _run(asgi_client.post("/api/projects", json={"name": "Stays Assigned"})).json()
    set_response = _run(
        asgi_client.patch(f"/api/projects/{project['id']}", json={"org_company_id": "active-co"})
    )
    assert set_response.status_code == 200

    for blank in ("", "   \t\n"):
        response = _run(
            asgi_client.patch(
                f"/api/projects/{project['id']}",
                json={"org_company_id": blank},
            )
        )
        assert response.status_code == 400, f"blank {blank!r} must be rejected, not accepted"
        body = response.json()
        assert "blank" in str(body).lower()

        row = store._connection.execute(  # noqa: SLF001
            "SELECT org_company_id FROM projects WHERE id = ?", (project["id"],)
        ).fetchone()
        assert row is not None
        assert row["org_company_id"] == "co_active", (
            f"blank {blank!r} must never clear the assignment; got {row['org_company_id']!r}"
        )


def test_patch_project_company_oversized_reference_returns_400_without_full_reflection(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    """Sol ADOPT 3: bound the reference length and never echo it back in full."""
    _insert_company(store, "co_acme", "acme", "ACME Corp")
    project = _run(asgi_client.post("/api/projects", json={"name": "Oversized"})).json()

    huge = "x" * 10_000
    response = _run(
        asgi_client.patch(f"/api/projects/{project['id']}", json={"org_company_id": huge})
    )
    assert response.status_code == 400
    raw_body = response.text
    assert huge not in raw_body, "the full 10k-char reference must never be reflected back"
    assert len(raw_body) < 2_000, f"error body should be compact, got {len(raw_body)} bytes"

    row = store._connection.execute(  # noqa: SLF001
        "SELECT org_company_id FROM projects WHERE id = ?", (project["id"],)
    ).fetchone()
    assert row is not None
    assert row["org_company_id"] is None
