"""Company goals + employees HTTP surface (JG2-BE).

Every assertion that matters is checked on the FAR SIDE (a direct read of the
database the route wrote), not on the response echo. The decisive route case is
``short_term`` without ``parent_goal_id``: 400 AND zero rows written.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.company_goals.seed_employees import seed_employees
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from tests.support.db_template import make_store

COMPANY_ID = "co_apiTest"


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    store = make_store(SqliteStore, tmp_path / "company_goals_api.db")
    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?,?,?,?,?)",
        (COMPANY_ID, "api-test", "API Test Co", "active", utc_now_iso()),
    )
    return store


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _call(database: SqliteStore, request: Callable[[httpx.AsyncClient], Any]) -> Any:
    async def driver() -> Any:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await request(client)
        finally:
            app.dependency_overrides.clear()

    return _run(driver())


def _far_side(database: SqliteStore, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return database._connection.execute(sql, params).fetchall()


def _goal_rows(database: SqliteStore) -> list[sqlite3.Row]:
    return _far_side(database, "SELECT * FROM company_goals ORDER BY created_at, id")


# ---------------------------------------------------------------------------
# DECISIVE: short_term requires a parent, through HTTP
# ---------------------------------------------------------------------------


def test_create_short_term_without_parent_is_400_and_writes_nothing(
    database: SqliteStore,
) -> None:
    async def request(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/api/company-goals",
            json={
                "org_company_id": COMPANY_ID,
                "title": "Close the quarter",
                "horizon": "short_term",
            },
        )

    response = _call(database, request)
    assert response.status_code == 400, response.text
    body = response.json()
    flat = str(body.get("error", body))
    assert "parent_goal_id" in flat

    assert _goal_rows(database) == [], "refused create must leave the table empty"


def test_goal_crud_round_trip_reads_back_from_the_database(database: SqliteStore) -> None:
    async def request(client: httpx.AsyncClient) -> dict[str, Any]:
        created = await client.post(
            "/api/company-goals",
            json={
                "org_company_id": COMPANY_ID,
                "title": "Become the operating system",
                "horizon": "long_term",
            },
        )
        assert created.status_code == 201, created.text
        parent = created.json()

        child = await client.post(
            "/api/company-goals",
            json={
                "org_company_id": COMPANY_ID,
                "title": "Ship the transcript pipeline",
                "horizon": "short_term",
                "parent_goal_id": parent["id"],
            },
        )
        assert child.status_code == 201, child.text

        listed = await client.get(
            "/api/company-goals", params={"org_company_id": COMPANY_ID}
        )
        assert listed.status_code == 200
        fetched = await client.get(f"/api/company-goals/{child.json()['id']}")
        assert fetched.status_code == 200
        patched = await client.patch(
            f"/api/company-goals/{child.json()['id']}",
            json={"status": "achieved", "title": "Shipped the transcript pipeline"},
        )
        assert patched.status_code == 200, patched.text
        missing = await client.get("/api/company-goals/cgl_nope")
        assert missing.status_code == 404
        return {
            "parent": parent,
            "child": child.json(),
            "listed": listed.json(),
            "fetched": fetched.json(),
            "patched": patched.json(),
        }

    out = _call(database, request)

    assert {g["id"] for g in out["listed"]["goals"]} == {out["parent"]["id"], out["child"]["id"]}
    assert out["fetched"]["parent_goal_id"] == out["parent"]["id"]
    assert out["patched"]["status"] == "achieved"

    rows = {row["id"]: row for row in _goal_rows(database)}
    assert set(rows) == {out["parent"]["id"], out["child"]["id"]}
    child_row = rows[out["child"]["id"]]
    assert child_row["horizon"] == "short_term"
    assert child_row["parent_goal_id"] == out["parent"]["id"]
    assert child_row["status"] == "achieved"
    assert child_row["title"] == "Shipped the transcript pipeline"
    assert child_row["updated_at"] >= child_row["created_at"]


def test_patch_to_short_term_without_parent_is_400_and_row_unchanged(
    database: SqliteStore,
) -> None:
    async def request(client: httpx.AsyncClient) -> httpx.Response:
        created = await client.post(
            "/api/company-goals",
            json={
                "org_company_id": COMPANY_ID,
                "title": "North star",
                "horizon": "long_term",
            },
        )
        assert created.status_code == 201, created.text
        return await client.patch(
            f"/api/company-goals/{created.json()['id']}", json={"horizon": "short_term"}
        )

    response = _call(database, request)
    assert response.status_code == 400, response.text
    rows = _goal_rows(database)
    assert len(rows) == 1
    assert rows[0]["horizon"] == "long_term"


def test_create_with_unknown_company_is_400(database: SqliteStore) -> None:
    async def request(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/api/company-goals",
            json={"org_company_id": "co_ghost", "title": "Nope", "horizon": "long_term"},
        )

    response = _call(database, request)
    assert response.status_code == 400, response.text
    assert _goal_rows(database) == []


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------


def test_get_employees_returns_the_seeded_roster(database: SqliteStore) -> None:
    seed_employees(CompanyGoalsStore(database))

    async def request(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get("/api/employees")

    response = _call(database, request)
    assert response.status_code == 200, response.text
    employees = response.json()["employees"]
    assert [e["name"] for e in employees] == ["Alice", "Bob", "Frank", "the operator"]
    assert all(e["jira_account_id"] is None for e in employees)
    assert {e["id"] for e in employees} == {
        str(row["id"]) for row in _far_side(database, "SELECT id FROM employees")
    }


def test_get_employees_is_empty_before_seeding(database: SqliteStore) -> None:
    async def request(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get("/api/employees")

    response = _call(database, request)
    assert response.status_code == 200
    assert response.json()["employees"] == []


def test_list_routes_are_bounded_by_limit(database: SqliteStore) -> None:
    """Unbounded list routes are a denial-of-service seam; every list caps."""
    seed_employees(CompanyGoalsStore(database))

    async def request(client: httpx.AsyncClient) -> dict[str, Any]:
        goal = await client.post(
            "/api/company-goals",
            json={"org_company_id": COMPANY_ID, "title": "Root", "horizon": "long_term"},
        )
        goal_id = goal.json()["id"]
        for index in range(3):
            created = await client.post(
                f"/api/company-goals/{goal_id}/jira-links",
                json={
                    "jira_project_key": "HOO",
                    "jira_issue_key": f"HOO-{index}",
                    "link_kind": "issue",
                },
            )
            assert created.status_code == 201, created.text
        return {
            "links_capped": await client.get(
                f"/api/company-goals/{goal_id}/jira-links", params={"limit": 2}
            ),
            "links_all": await client.get(f"/api/company-goals/{goal_id}/jira-links"),
            "employees_capped": await client.get("/api/employees", params={"limit": 1}),
            "employees_all": await client.get("/api/employees"),
            "links_bad_limit": await client.get(
                f"/api/company-goals/{goal_id}/jira-links", params={"limit": 0}
            ),
            "employees_bad_limit": await client.get("/api/employees", params={"limit": 0}),
        }

    out = _call(database, request)
    assert len(out["links_capped"].json()["links"]) == 2
    assert len(out["links_all"].json()["links"]) == 3
    assert len(out["employees_capped"].json()["employees"]) == 1
    assert len(out["employees_all"].json()["employees"]) == 4
    assert out["links_bad_limit"].status_code == 422
    assert out["employees_bad_limit"].status_code == 422


# ---------------------------------------------------------------------------
# Goal <-> Jira links
# ---------------------------------------------------------------------------


def test_jira_link_endpoints_create_list_delete(database: SqliteStore) -> None:
    async def request(client: httpx.AsyncClient) -> dict[str, Any]:
        goal = await client.post(
            "/api/company-goals",
            json={"org_company_id": COMPANY_ID, "title": "Root", "horizon": "long_term"},
        )
        goal_id = goal.json()["id"]
        created = await client.post(
            f"/api/company-goals/{goal_id}/jira-links",
            json={"jira_project_key": "HOO", "link_kind": "project"},
        )
        assert created.status_code == 201, created.text
        duplicate = await client.post(
            f"/api/company-goals/{goal_id}/jira-links",
            json={"jira_project_key": "HOO", "link_kind": "project"},
        )
        issue = await client.post(
            f"/api/company-goals/{goal_id}/jira-links",
            json={
                "jira_project_key": "HOO",
                "jira_issue_key": "HOO-42",
                "link_kind": "issue",
            },
        )
        assert issue.status_code == 201, issue.text
        listed = await client.get(f"/api/company-goals/{goal_id}/jira-links")
        deleted = await client.delete(
            f"/api/company-goals/{goal_id}/jira-links/{created.json()['id']}"
        )
        deleted_again = await client.delete(
            f"/api/company-goals/{goal_id}/jira-links/{created.json()['id']}"
        )
        orphan = await client.post(
            "/api/company-goals/cgl_ghost/jira-links",
            json={"jira_project_key": "ACM", "link_kind": "project"},
        )
        return {
            "goal_id": goal_id,
            "created": created.json(),
            "duplicate_status": duplicate.status_code,
            "issue": issue.json(),
            "listed": listed.json(),
            "deleted_status": deleted.status_code,
            "deleted_again_status": deleted_again.status_code,
            "orphan_status": orphan.status_code,
        }

    out = _call(database, request)

    assert out["duplicate_status"] == 400
    assert out["orphan_status"] == 404
    assert {row["id"] for row in out["listed"]["links"]} == {
        out["created"]["id"],
        out["issue"]["id"],
    }
    assert out["deleted_status"] == 200
    assert out["deleted_again_status"] == 404

    rows = _far_side(database, "SELECT * FROM company_goal_jira_links")
    assert [row["id"] for row in rows] == [out["issue"]["id"]]
    assert rows[0]["jira_issue_key"] == "HOO-42"
    assert rows[0]["goal_id"] == out["goal_id"]


# ---------------------------------------------------------------------------
# JG2-E13c — six companies through the live orgdims route
# ---------------------------------------------------------------------------


def test_orgdims_companies_route_returns_all_six(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: SqliteStore
) -> None:
    from omniagentos.api.routes import orgdims as orgdims_routes
    from omniagentos.orgdims.service import OrgDimsService

    service = OrgDimsService(db_path=str(tmp_path / "orgdims-six.db"))
    service.ensure_seeded()
    monkeypatch.setattr(orgdims_routes, "_SERVICE", service)

    async def request(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get("/api/orgdims/companies")

    response = _call(database, request)
    assert response.status_code == 200, response.text
    slugs = {c["slug"] for c in response.json()["companies"]}
    assert slugs == {
        "initech",
        "globex",
        "acmeuni",
        "hooli",
        "omniagentos",
        "personal",
    }
