"""HTTP contract for the Team Work OS routes."""

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
from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso
from omniagentos.sessions import token
from omniagentos.team.scoring import BASELINE_SOURCE
from omniagentos.team.store import TeamStore
from tests.support.db_template import migrated_db


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    # Hermetic fleet ledger: the scoreboard's OVERALL block must never read the
    # serving checkout's real var/loopqueue/ledger.jsonl from a unit test.
    monkeypatch.setenv("OMNI_TEAM_FLEET_LEDGER", str(tmp_path / "no-such-ledger.jsonl"))
    db_path = migrated_db(CollabStore, tmp_path / "team_api.db")
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


def _employee(api: dict[str, Any], employee_id: str) -> None:
    CompanyGoalsStore(api["store"]).ensure_employee(
        employee_id=employee_id, name=employee_id, role="team"
    )


def test_board_buckets_shape_and_owner_filter(api: dict[str, Any]) -> None:
    _employee(api, "emp_bob")
    _employee(api, "emp_alice")
    collab: CollabStore = api["collab"]
    collab.create_board_task(BoardTask(title="Ready", owner_employee_id="emp_bob"))
    collab.create_board_task(BoardTask(title="Other", owner_employee_id="emp_alice"))
    collab.create_board_task(
        BoardTask(title="Urgent", owner_employee_id="emp_bob", priority="urgent")
    )

    all_queues = _run(api["client"].get("/api/team/board"))
    assert all_queues.status_code == 200, all_queues.text
    bob = all_queues.json()["emp_bob"]
    assert set(bob) == {
        "employee_id",
        "ready",
        "active",
        "blocked",
        "review",
        "done_today",
        "counts",
        "ready_below_5",
        "active_below_5",
    }
    assert bob["counts"]["ready"] == 2
    assert bob["ready_below_5"] is True
    assert bob["active_below_5"] is True
    # Every queue card carries its priority, urgent-first (the store ranks it).
    assert [(card["title"], card["priority"]) for card in bob["ready"]] == [
        ("Urgent", "urgent"),
        ("Ready", "normal"),
    ]
    assert all_queues.json()["pool"] == {
        "cards": [],
        "depth": 0,
        "low": True,
        "truncated": False,
    }

    filtered = _run(api["client"].get("/api/team/board?owner=emp_bob"))
    assert list(filtered.json()) == ["emp_bob", "pool"]
    assert filtered.json()["emp_bob"]["ready"][0]["title"] == "Urgent"

    live_board = _run(api["client"].get("/api/board?owner=emp_bob"))
    assert live_board.status_code == 200, live_board.text
    assert [card["owner_employee_id"] for card in live_board.json()] == [
        "emp_bob",
        "emp_bob",
    ]


def test_board_unknown_owner_returns_404_but_known_empty_owner_returns_200(
    api: dict[str, Any],
) -> None:
    _employee(api, "emp_bob")
    known = _run(api["client"].get("/api/team/board?owner=emp_bob"))
    assert known.status_code == 200, known.text
    assert known.json()["emp_bob"]["counts"]["ready"] == 0

    unknown = _run(api["client"].get("/api/team/board?owner=emp_unknown"))
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"]["code"] == "not_found"


def test_tree_nests_company_goal_task_and_subtask(api: dict[str, Any]) -> None:
    store = api["store"]
    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?,?,?,?,?)",
        ("co_team", "team", "Team Co", "active", utc_now_iso()),
    )
    goal = CompanyGoalsStore(store).create_goal(
        goal_id="cgl_team", org_company_id="co_team", title="Ship", horizon="long_term"
    )
    collab: CollabStore = api["collab"]
    parent = BoardTask(title="Parent", goal_id=goal["id"], ref="TEAM-1", size="L")
    child = BoardTask(title="Child", goal_id=goal["id"], parent_task_id=parent.id, size="S")
    collab.create_board_task(parent)
    collab.create_board_task(child)

    response = _run(api["client"].get("/api/team/tree"))
    assert response.status_code == 200, response.text
    task = response.json()["companies"][0]["goals"][0]["tasks"][0]
    assert task["id"] == parent.id
    assert task["ref"] == "TEAM-1"
    assert task["subtasks"][0]["id"] == child.id


def test_verify_happy_self_refused_and_mechanical_path(api: dict[str, Any]) -> None:
    _employee(api, "emp_bob")
    collab: CollabStore = api["collab"]
    team: TeamStore = api["team"]
    human = BoardTask(title="Human", owner_employee_id="emp_bob", acceptance_criteria="works")
    collab.create_board_task(human)
    team.add_evidence(kind="doc", ref="human-doc", task_id=human.id)
    collab.update_board_task(human.id, {"status": BoardTaskStatus.DONE.value})

    self_verify = _run(
        api["client"].post(f"/api/team/tasks/{human.id}/verify", json={"verifier": "emp_bob"})
    )
    assert self_verify.status_code == 400
    verified = _run(
        api["client"].post(f"/api/team/tasks/{human.id}/verify", json={"verifier": "emp_alice"})
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["verified_by"] == "emp_alice"

    mechanical = BoardTask(
        title="Mechanical", owner_employee_id="emp_bob", acceptance_criteria="passes"
    )
    collab.create_board_task(mechanical)
    team.add_evidence(kind="test_run", ref="test-run-1", task_id=mechanical.id)
    collab.update_board_task(mechanical.id, {"status": BoardTaskStatus.DONE.value})
    mechanical_verify = _run(
        api["client"].post(
            f"/api/team/tasks/{mechanical.id}/verify", json={"verifier": "emp_bob"}
        )
    )
    assert mechanical_verify.status_code == 200, mechanical_verify.text


def test_evidence_post_get_and_dedupes_on_the_same_task(api: dict[str, Any]) -> None:
    card = BoardTask(title="Evidence")
    api["collab"].create_board_task(card)
    body = {"kind": "commit", "ref": "abc123", "repo": "repo", "actor": "emp_owner", "meta": {"x": 1}}
    first = _run(api["client"].post(f"/api/team/tasks/{card.id}/evidence", json=body))
    second = _run(api["client"].post(f"/api/team/tasks/{card.id}/evidence", json=body))
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["attribution"] == "manual"
    evidence = _run(api["client"].get(f"/api/team/tasks/{card.id}/evidence"))
    assert [row["id"] for row in evidence.json()] == [first.json()["id"]]


def test_evidence_post_attaches_existing_deterministic_unattributed_row(
    api: dict[str, Any],
) -> None:
    card = BoardTask(title="Evidence target")
    api["collab"].create_board_task(card)
    evidence_id = api["team"].add_evidence(
        kind="commit",
        ref="attach-me",
        repo="repo",
        actor="collector",
        attribution="deterministic",
    )

    response = _run(
        api["client"].post(
            f"/api/team/tasks/{card.id}/evidence",
            json={"kind": "commit", "ref": "attach-me", "repo": "repo", "actor": "emp_owner"},
        )
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == evidence_id
    assert response.json()["task_id"] == card.id
    assert response.json()["attribution"] == "manual"
    assert response.json()["actor"] == "emp_owner"
    events = api["team"].list_events(card.id)
    assert any(
        event["event"] == "evidence"
        and event["actor"] == "emp_owner"
        and event["note"] == "attached commit:attach-me"
        for event in events
    )


def test_evidence_post_conflicts_when_artifact_belongs_to_a_different_task(
    api: dict[str, Any],
) -> None:
    owner = BoardTask(title="Existing owner")
    target = BoardTask(title="URL target")
    api["collab"].create_board_task(owner)
    api["collab"].create_board_task(target)
    evidence_id = api["team"].add_evidence(
        kind="commit",
        ref="spoken-for",
        repo="repo",
        task_id=owner.id,
        attribution="deterministic",
    )

    response = _run(
        api["client"].post(
            f"/api/team/tasks/{target.id}/evidence",
            json={"kind": "commit", "ref": "spoken-for", "repo": "repo", "actor": "emp_owner"},
        )
    )
    assert response.status_code == 409, response.text
    assert response.json() == {
        "detail": "evidence_exists",
        "evidence_id": evidence_id,
        "task_id": owner.id,
    }


def test_unverify_baseline_immutable_is_a_400_not_a_500(api: dict[str, Any]) -> None:
    baseline = BoardTask(title="Baseline", source=BASELINE_SOURCE)
    api["collab"].create_board_task(baseline)
    response = _run(
        api["client"].post(f"/api/team/tasks/{baseline.id}/unverify", json={"actor": "emp_owner"})
    )
    assert response.status_code == 400, response.text
    assert "baseline_immutable" in response.text


def test_reattribution_and_unattributed_list(api: dict[str, Any]) -> None:
    card = BoardTask(title="Target")
    api["collab"].create_board_task(card)
    evidence_id = api["team"].add_evidence(kind="commit", ref="orphan", repo="repo")
    inbox = _run(api["client"].get("/api/team/evidence/unattributed"))
    assert [row["id"] for row in inbox.json()] == [evidence_id]
    moved = _run(
        api["client"].patch(
            f"/api/team/evidence/{evidence_id}", json={"task_id": card.id, "actor": "emp_owner"}
        )
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["task_id"] == card.id
    assert _run(api["client"].get("/api/team/evidence/unattributed")).json() == []


def test_collab_ref_conflict_blocked_reason_and_forbidden_verification_patch(
    api: dict[str, Any],
) -> None:
    _employee(api, "emp_bob")
    client: httpx.AsyncClient = api["client"]
    first = _run(client.post("/api/collab/board", json={"title": "One", "ref": "DUP-1"}))
    assert first.status_code == 201
    conflict = _run(client.post("/api/collab/board", json={"title": "Two", "ref": "DUP-1"}))
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "ref_conflict"}

    task_id = first.json()["id"]
    assigned = _run(
        client.patch(f"/api/collab/board/{task_id}", json={"owner_employee_id": "emp_bob"})
    )
    assert assigned.status_code == 200
    blocked = _run(
        client.patch(
            f"/api/collab/board/{task_id}",
            json={"status": "blocked", "blocked_reason": "waiting for review"},
        )
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["blocked_reason"] == "waiting for review"
    forbidden = _run(client.patch(f"/api/collab/board/{task_id}", json={"verified_at": "now"}))
    assert forbidden.status_code == 400


def test_collab_bad_goal_is_a_clean_400_on_create_and_patch(api: dict[str, Any]) -> None:
    client: httpx.AsyncClient = api["client"]
    invalid_create = _run(
        client.post(
            "/api/collab/board",
            json={"title": "Bad goal", "goal_id": "cgl_missing"},
        )
    )
    assert invalid_create.status_code == 400, invalid_create.text
    assert "unknown goal" in invalid_create.text

    created = _run(client.post("/api/collab/board", json={"title": "Legacy card"}))
    assert created.status_code == 201, created.text
    invalid_patch = _run(
        client.patch(
            f"/api/collab/board/{created.json()['id']}",
            json={"goal_id": "cgl_missing"},
        )
    )
    assert invalid_patch.status_code == 400, invalid_patch.text
    assert "unknown goal" in invalid_patch.text
