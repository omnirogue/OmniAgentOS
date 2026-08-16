"""Universal pool query and HTTP claim-to-owned-queue flow."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.contracts import BASELINE_SOURCE, BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso
from omniagentos.sessions import token
from omniagentos.team.store import TeamStore
from tests.support.db_template import migrated_db


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def _goal(pool_api: dict[str, Any]) -> str:
    store = pool_api["store"]
    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
        ("co_pool", "pool", "Pool Co", "active", utc_now_iso()),
    )
    goal = CompanyGoalsStore(store).create_goal(
        goal_id="cgl_pool", org_company_id="co_pool", title="Drain the pool", horizon="quarter"
    )
    return str(goal["id"])


@pytest.fixture
def pool_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    db_path = migrated_db(CollabStore, tmp_path / "pool-api.db")
    collab = CollabStore(db_path)
    store = collab._store
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_collab_store] = lambda: collab
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Session-Token": token.load_or_create_token()},
    )
    try:
        yield {"client": client, "collab": collab, "store": store, "team": TeamStore(store)}
    finally:
        app.dependency_overrides.clear()
        _run(client.aclose())


def test_pool_excludes_every_non_pool_card(
    collab_store: CollabStore,
    team_store: TeamStore,
    employees: dict[str, str],
) -> None:
    goals = CompanyGoalsStore(collab_store._store)
    collab_store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
        ("co_pool", "pool", "Pool Co", "active", utc_now_iso()),
    )
    goal = goals.create_goal(
        goal_id="cgl_pool", org_company_id="co_pool", title="Drain", horizon="quarter"
    )
    contract = {"goal_id": goal["id"], "acceptance_criteria": "done means done"}
    included = BoardTask(title="Available", **contract)
    owned = BoardTask(title="Assigned", owner_employee_id=employees["bob"], **contract)
    parent = BoardTask(title="Parent", **contract)
    subtask = BoardTask(title="Subtask", parent_task_id=parent.id, **contract)
    baseline = BoardTask(title="Baseline", source=BASELINE_SOURCE, **contract)
    archived = BoardTask(title="Archived", **contract)
    legacy = BoardTask(title="Legacy ownerless agent card")
    blank_acceptance = BoardTask(
        title="Blank acceptance", goal_id=goal["id"], acceptance_criteria="   "
    )
    non_open = [
        BoardTask(title=f"Not open: {status.value}", status=status, **contract)
        for status in BoardTaskStatus
        if status is not BoardTaskStatus.OPEN
    ]
    for card in (
        included,
        owned,
        parent,
        subtask,
        baseline,
        archived,
        legacy,
        blank_acceptance,
        *non_open,
    ):
        collab_store.create_board_task(card)
    collab_store.update_board_task(archived.id, {"archived_at": utc_now_iso()})

    ids = {card.id for card in team_store.pool_cards()}
    assert included.id in ids
    assert parent.id in ids
    assert owned.id not in ids
    assert subtask.id not in ids
    assert baseline.id not in ids
    assert archived.id not in ids
    assert legacy.id not in ids
    assert blank_acceptance.id not in ids
    assert ids.isdisjoint(card.id for card in non_open)


@pytest.mark.parametrize(
    ("depth", "low", "visible", "truncated"),
    [(9, True, 9, False), (10, False, 10, False), (55, False, 50, True)],
)
def test_pool_payload_depth_low_boundary(
    pool_api: dict[str, Any], depth: int, low: bool, visible: int, truncated: bool
) -> None:
    collab: CollabStore = pool_api["collab"]
    goal_id = _goal(pool_api)
    for index in range(depth):
        collab.create_board_task(
            BoardTask(
                title=f"Pool {index}",
                goal_id=goal_id,
                acceptance_criteria="meets the pool contract",
            )
        )

    response = _run(pool_api["client"].get("/api/team/board"))
    assert response.status_code == 200, response.text
    pool = response.json()["pool"]
    assert set(pool) == {"cards", "depth", "low", "truncated"}
    assert pool["depth"] == depth
    assert pool["low"] is low
    assert pool["truncated"] is truncated
    assert len(pool["cards"]) == visible
    assert set(pool["cards"][0]) == {
        "id",
        "title",
        "ref",
        "status",
        "size",
        "priority",
        # Multi-company Work OS (2026-08-13): owner + company are additive
        # wire fields; a card with no goal serves them as null, never absent.
        "owner_employee_id",
        "company_slug",
        "company_name",
        # v4 Work-vs-Tasks (2026-08-13): the discriminator + deadline are
        # additive wire fields too — null when unset, never absent.
        "source",
        "due_date",
    }


def test_ownerless_create_claim_moves_pool_card_to_claimants_active_bucket(
    pool_api: dict[str, Any],
) -> None:
    store = pool_api["store"]
    goals = CompanyGoalsStore(store)
    goals.ensure_employee(employee_id="emp_bob", name="Bob", role="candidate-author")
    goal_id = _goal(pool_api)
    client: httpx.AsyncClient = pool_api["client"]

    legacy = _run(client.post("/api/collab/board", json={"title": "Legacy agent card"}))
    assert legacy.status_code == 201, legacy.text
    assert legacy.json()["goal_id"] is None

    invalid = _run(client.post("/api/team/tasks", json={"title": "Missing contract"}))
    assert invalid.status_code == 400
    unknown = _run(
        client.post(
            "/api/team/tasks",
            json={
                "title": "Unknown goal",
                "goal_id": "cgl_missing",
                "acceptance_criteria": "must not reach sqlite",
            },
        )
    )
    assert unknown.status_code == 400
    assert "unknown goal" in unknown.text

    created = _run(
        client.post(
            "/api/team/tasks",
            json={
                "title": "Claim me",
                "goal_id": goal_id,
                "acceptance_criteria": "required tests pass",
                "ref": "POOL-1",
            },
        )
    )
    assert created.status_code == 201, created.text
    card_id = created.json()["id"]
    before = _run(client.get("/api/team/board?owner=emp_bob")).json()
    assert [card["id"] for card in before["pool"]["cards"]] == [card_id]
    assert before["emp_bob"]["active"] == []

    claimed = _run(
        client.post(
            f"/api/collab/board/{card_id}/claim",
            json={"agent_id": "human:emp_bob", "employee_id": "emp_bob"},
            headers={"X-Omni-Authenticated-Principal": "emp_bob"},
        )
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json() == {
        "success": True,
        "owner_employee_id": "emp_bob",
        "claimed_by": "human:emp_bob",
    }

    after = _run(client.get("/api/team/board?owner=emp_bob")).json()
    assert after["pool"]["cards"] == []
    assert [card["id"] for card in after["emp_bob"]["active"]] == [card_id]
    row = pool_api["collab"].get_board_task(card_id)
    assert row is not None
    assert row["status"] == BoardTaskStatus.CLAIMED.value
    assert row["owner_employee_id"] == "emp_bob"
    assign = [
        event for event in pool_api["team"].list_events(card_id) if event["event"] == "assign"
    ]
    assert assign[-1]["note"] == "owner:emp_bob"


def test_owned_and_baseline_cards_cannot_be_claimed_with_an_ownership_transfer(
    pool_api: dict[str, Any],
) -> None:
    goals = CompanyGoalsStore(pool_api["store"])
    for employee_id in ("emp_victim", "emp_thief"):
        goals.ensure_employee(employee_id=employee_id, name=employee_id, role="team")
    collab: CollabStore = pool_api["collab"]
    victim = BoardTask(title="Victim card", owner_employee_id="emp_victim")
    baseline = BoardTask(title="Baseline", source=BASELINE_SOURCE)
    collab.create_board_task(victim)
    collab.create_board_task(baseline)
    client: httpx.AsyncClient = pool_api["client"]

    for card in (victim, baseline):
        response = _run(
            client.post(
                f"/api/collab/board/{card.id}/claim",
                json={"agent_id": "human:emp_thief", "employee_id": "emp_thief"},
                headers={"X-Omni-Authenticated-Principal": "emp_thief"},
            )
        )
        assert response.status_code == 409, response.text
    assert collab.get_board_task(victim.id)["owner_employee_id"] == "emp_victim"
    assert collab.get_board_task(baseline.id)["owner_employee_id"] is None


def test_claim_employee_is_bound_to_principal_and_must_be_on_roster(
    pool_api: dict[str, Any],
) -> None:
    collab: CollabStore = pool_api["collab"]
    goals = CompanyGoalsStore(pool_api["store"])
    for employee_id in ("emp_other", "emp_claimant"):
        goals.ensure_employee(employee_id=employee_id, name=employee_id, role="team")
    card = BoardTask(title="Principal-bound claim")
    collab.create_board_task(card)
    client: httpx.AsyncClient = pool_api["client"]

    mismatch = _run(
        client.post(
            f"/api/collab/board/{card.id}/claim",
            json={"agent_id": "human:emp_other", "employee_id": "emp_other"},
            headers={"X-Omni-Authenticated-Principal": "emp_claimant"},
        )
    )
    assert mismatch.status_code == 400

    missing = _run(
        client.post(
            f"/api/collab/board/{card.id}/claim",
            json={"agent_id": "human:emp_ghost", "employee_id": "emp_ghost"},
            headers={"X-Omni-Authenticated-Principal": "emp_ghost"},
        )
    )
    assert missing.status_code == 404
    assert "employee not found" in missing.text

    # An unmapped principal with NO body-asserted employee claims an ordinary
    # (non-pool) card on the legacy ownerless path — this is the base behavior
    # every existing human claim relies on (INT-03). The mapping refusal is
    # reserved for pool cards and body-asserted employee ids.
    unmapped = _run(
        client.post(
            f"/api/collab/board/{card.id}/claim",
            json={"agent_id": "human:unknown"},
            headers={"X-Omni-Authenticated-Principal": "unknown@example.test"},
        )
    )
    assert unmapped.status_code == 200
    assert unmapped.json()["success"] is True
    assert unmapped.json()["owner_employee_id"] is None


def test_pool_claim_requires_owner_but_non_pool_agent_claim_remains_valid(
    pool_api: dict[str, Any],
) -> None:
    goal_id = _goal(pool_api)
    collab: CollabStore = pool_api["collab"]
    pool_card = BoardTask(
        title="Needs a human owner",
        goal_id=goal_id,
        acceptance_criteria="must stay visible",
    )
    agent_card = BoardTask(title="Legacy agent work")
    collab.create_board_task(pool_card)
    collab.create_board_task(agent_card)
    client: httpx.AsyncClient = pool_api["client"]

    refused = _run(
        client.post(
            f"/api/collab/board/{pool_card.id}/claim",
            json={"agent_id": "agent:no-principal"},
        )
    )
    assert refused.status_code == 400
    assert "pool card requires a resolvable employee owner" in refused.text
    assert collab.get_board_task(pool_card.id)["status"] == BoardTaskStatus.OPEN.value

    accepted = _run(
        client.post(
            f"/api/collab/board/{agent_card.id}/claim",
            json={"agent_id": "agent:no-principal"},
        )
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {
        "success": True,
        "owner_employee_id": None,
        "claimed_by": "agent:no-principal",
    }


def test_email_principal_map_resolves_to_roster_employee(
    pool_api: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omniagentos.api.routes import collab as collab_routes

    goals = CompanyGoalsStore(pool_api["store"])
    goals.ensure_employee(employee_id="emp_email", name="Email", role="team")
    mapping = tmp_path / "team_github_map.yaml"
    mapping.write_text("person@example.test: emp_email\n", encoding="utf-8")
    monkeypatch.setattr(collab_routes, "_TEAM_GITHUB_MAP_PATH", mapping)
    card = BoardTask(title="Email-mapped card")
    pool_api["collab"].create_board_task(card)

    response = _run(
        pool_api["client"].post(
            f"/api/collab/board/{card.id}/claim",
            json={"agent_id": "human:emp_email", "employee_id": "emp_email"},
            headers={"X-Omni-Authenticated-Principal": "person@example.test"},
        )
    )
    assert response.status_code == 200, response.text
    assert response.json()["owner_employee_id"] == "emp_email"
    assert pool_api["collab"].get_board_task(card.id)["owner_employee_id"] == "emp_email"


def test_claim_cas_allows_self_claim_but_refuses_transfer_and_baseline(
    pool_api: dict[str, Any],
) -> None:
    collab: CollabStore = pool_api["collab"]
    goals = CompanyGoalsStore(pool_api["store"])
    for employee_id in ("emp_ownere", "emp_other"):
        goals.ensure_employee(employee_id=employee_id, name=employee_id, role="team")
    goal_id = _goal(pool_api)
    owned = BoardTask(title="Already mine", owner_employee_id="emp_ownere")
    baseline = BoardTask(
        title="Baseline pool-shaped",
        source=BASELINE_SOURCE,
        goal_id=goal_id,
        acceptance_criteria="otherwise pool-conformant",
    )
    collab.create_board_task(owned)
    collab.create_board_task(baseline)

    assert collab.claim_task(
        owned.id,
        "human:emp_ownere",
        0,
        actor="emp_ownere",
        owner_employee_id="emp_ownere",
    )
    assert collab.release_claim(owned.id)
    assert not collab.claim_task(
        owned.id,
        "human:emp_other",
        2,
        actor="emp_other",
        owner_employee_id="emp_other",
    )
    assert not collab.claim_task(
        baseline.id,
        "human:emp_ownere",
        0,
        actor="emp_ownere",
        owner_employee_id="emp_ownere",
    )


def test_release_can_explicitly_return_a_claimed_card_to_the_pool(
    pool_api: dict[str, Any],
) -> None:
    goals = CompanyGoalsStore(pool_api["store"])
    goals.ensure_employee(employee_id="emp_x", name="X", role="team")
    goal_id = _goal(pool_api)
    collab: CollabStore = pool_api["collab"]
    card = BoardTask(
        title="Return me", goal_id=goal_id, acceptance_criteria="available for reassignment"
    )
    collab.create_board_task(card)
    assert collab.claim_task(
        card.id,
        "human:emp_x",
        0,
        actor="emp_x",
        owner_employee_id="emp_x",
    )

    assert collab.release_claim(card.id, actor="emp_x", return_to_pool=True)
    row = collab.get_board_task(card.id)
    assert row["owner_employee_id"] is None
    assert card.id in {candidate.id for candidate in pool_api["team"].pool_cards()}
    assign = [
        event for event in pool_api["team"].list_events(card.id) if event["event"] == "assign"
    ]
    assert assign[-1]["note"] == "owner:none"


def test_release_claim_http_returns_updated_card_and_honors_version(
    pool_api: dict[str, Any],
) -> None:
    collab: CollabStore = pool_api["collab"]
    card = BoardTask(title="Release by HTTP")
    collab.create_board_task(card)
    assert collab.claim_task(card.id, "agent:x", 0)

    stale = _run(
        pool_api["client"].post(
            f"/api/collab/board/{card.id}/release",
            json={"return_to_pool": False, "expect_version": 0},
        )
    )
    assert stale.status_code == 409
    released = _run(
        pool_api["client"].post(
            f"/api/collab/board/{card.id}/release",
            json={"return_to_pool": False, "expect_version": 1},
        )
    )
    assert released.status_code == 200, released.text
    assert released.json()["status"] == BoardTaskStatus.OPEN.value
    assert released.json()["claim_version"] == 2

    missing = _run(
        pool_api["client"].post(
            "/api/collab/board/bt_missing/release",
            json={"return_to_pool": False},
        )
    )
    assert missing.status_code == 404
