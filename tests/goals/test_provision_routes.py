from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.db.store import SqliteStore
from omniagentos.goals.provision import validate_setpoint
from omniagentos.sessions import token
from omniagentos.steward.store import StewardStore


def _target(
    *, source: str = "mission_direct", comparator: str = ">=", target: float = 0.8
) -> dict[str, Any]:
    return {
        "metric_source": source,
        "comparator": comparator,
        "target": target,
        "sustain": {"periods": 2, "window": 60},
        "effort": {"max_cycles": 3, "max_work_items": 1},
    }


def test_validate_setpoint_refuses_unknown_weak_deep_and_invalid_effort(tmp_path) -> None:
    steward = StewardStore(SqliteStore(str(tmp_path / "provision.db")))
    root = steward.upsert_goal({"name": "root", "north_star": {}, "target": _target()})
    child = steward.upsert_goal(
        {
            "name": "child",
            "north_star": {},
            "parent_goal_id": root["id"],
            "target": _target(target=0.9),
        }
    )
    grandchild = steward.upsert_goal(
        {
            "name": "grandchild",
            "north_star": {},
            "parent_goal_id": child["id"],
            "target": _target(target=0.95),
        }
    )
    with pytest.raises(ValueError):
        validate_setpoint(
            {"name": "bad-source", "target": _target(source="not_registered")}, store=steward
        )
    with pytest.raises(ValueError):
        validate_setpoint(
            {
                "name": "weak",
                "parent_goal_id": root["id"],
                "target": _target(comparator="<=", target=0.7),
            },
            store=steward,
        )
    with pytest.raises(ValueError):
        validate_setpoint(
            {
                "name": "too-deep",
                "parent_goal_id": grandchild["id"],
                "target": _target(target=0.99),
            },
            store=steward,
        )
    for effort in (
        {"max_cycles": 0, "max_work_items": 1},
        {"max_cycles": 1, "max_work_items": 0},
        {"max_cycles": 1, "budget_usd": -1},
        {"max_cycles": 1},
    ):
        spec = _target()
        spec["effort"] = effort
        with pytest.raises(ValueError):
            validate_setpoint({"name": "bad-effort", "target": spec}, store=steward)
    impossible = _target()
    impossible["sustain"] = {"periods": 4, "window": 60}
    impossible["effort"] = {"max_cycles": 3, "max_work_items": 1}
    with pytest.raises(ValueError, match="periods must not exceed"):
        validate_setpoint({"name": "impossible", "target": impossible}, store=steward)
    for invalid_window in (None, True, "60", -1, 0, 0.5):
        invalid = _target()
        invalid["sustain"] = {"periods": 2, "window": invalid_window}
        with pytest.raises(ValueError, match="window"):
            validate_setpoint({"name": "bad-window", "target": invalid}, store=steward)
    whole_float = _target()
    whole_float["sustain"] = {"periods": 2, "window": 60.0}
    validate_setpoint({"name": "float-window", "target": whole_float}, store=steward)


def test_validate_setpoint_accepts_tightened_depth_two_child(tmp_path) -> None:
    steward = StewardStore(SqliteStore(str(tmp_path / "valid-provision.db")))
    root = steward.upsert_goal({"name": "root", "north_star": {}, "target": _target()})
    child = {
        "name": "child",
        "parent_goal_id": root["id"],
        "target": _target(target=0.9),
    }
    validate_setpoint(child, store=steward)
    child = steward.upsert_goal({**child, "north_star": {}})
    grandchild = {
        "name": "grandchild",
        "parent_goal_id": child["id"],
        "target": _target(target=0.95),
    }
    validate_setpoint(grandchild, store=steward)


@pytest.mark.real_auth
def test_goal_mutation_routes_deny_system_principal_through_the_live_gate(
    tmp_path,
) -> None:
    """Create/pause/target-PATCH refuse a raw machine bearer through the
    REAL app-level gate (``real_auth`` disables the suite-wide bypass), i.e.
    ``require_session_token`` -> ``_system_route_is_denied``, not just the
    in-handler ``principal is None`` check."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    session_token = token.load_or_create_token()
    store = SqliteStore(str(tmp_path / "system-deny.db"))
    steward = StewardStore(store)
    goal = steward.upsert_goal({"name": "root", "north_star": {}, "target": _target()})
    app.dependency_overrides[get_store] = lambda: store
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    system_headers = {"X-Session-Token": session_token}
    try:
        create = asyncio.run(
            client.post(
                "/api/goals",
                json={"name": "sys-created", "north_star": {}, "target": _target()},
                headers=system_headers,
            )
        )
        pause = asyncio.run(client.post(f"/api/goals/{goal['id']}/pause", headers=system_headers))
        target = asyncio.run(
            client.patch(
                f"/api/goals/{goal['id']}/target",
                json={"target": _target(target=0.9)},
                headers=system_headers,
            )
        )
        no_token_create = asyncio.run(
            client.post(
                "/api/goals", json={"name": "no-token", "north_star": {}, "target": _target()}
            )
        )
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()
        monkeypatch.undo()

    for resp in (create, pause, target):
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "system_principal_forbidden"
    assert no_token_create.status_code == 401
    assert steward.get_goal_by_name("sys-created") is None


@pytest.mark.real_auth
def test_create_goal_refuses_forged_graduation_and_duplicate_name(tmp_path) -> None:
    """CreateGoalRequest cannot mass-assign status/graduated_at; an existing
    name is refused 409, never silently upserted."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    session_token = token.load_or_create_token()
    store = SqliteStore(str(tmp_path / "forged-graduation.db"))
    app.dependency_overrides[get_store] = lambda: store
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    headers = {"X-Session-Token": session_token, "X-Omni-Authenticated-Principal": "operator"}
    try:
        forged = asyncio.run(
            client.post(
                "/api/goals",
                json={
                    "name": "forged",
                    "north_star": {},
                    "target": _target(),
                    "status": "graduated",
                    "graduated_at": "2020-01-01T00:00:00Z",
                },
                headers=headers,
            )
        )
        first = asyncio.run(
            client.post(
                "/api/goals",
                json={"name": "dup", "north_star": {}, "target": _target()},
                headers=headers,
            )
        )
        duplicate = asyncio.run(
            client.post(
                "/api/goals",
                json={"name": "dup", "north_star": {}, "target": _target(target=0.99)},
                headers=headers,
            )
        )
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()
        monkeypatch.undo()

    assert forged.status_code == 201, forged.text
    assert forged.json()["status"] != "graduated"
    assert forged.json()["graduated_at"] is None
    assert first.status_code == 201
    assert duplicate.status_code == 409
    steward = StewardStore(store)
    assert steward.get_goal_by_name("dup")["target"]["target"] == pytest.approx(0.8)


def test_goal_loop_routes_tree_and_target_auth(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    steward = StewardStore(store)
    goal = steward.upsert_goal({"name": "root", "north_star": {}, "target": _target()})
    steward.append_goal_reading(
        {
            "goal_id": goal["id"],
            "cycle": 1,
            "value": 0.9,
            "met": 1,
            "captured_at": "2026-08-14T12:00:00Z",
        }
    )
    steward.append_goal_reading(
        {
            "goal_id": goal["id"],
            "cycle": 2,
            "value": 0.95,
            "met": 1,
            "captured_at": "2026-08-14T12:01:00Z",
        }
    )
    app.dependency_overrides[get_store] = lambda: store
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    monkeypatch.setattr("omniagentos.sessions.token.verify_token", lambda token: token == "valid")
    try:
        tree = asyncio.run(client.get(f"/api/goals/tree/{goal['id']}"))
        steward.append_goal_reading(
            {
                "goal_id": goal["id"],
                "cycle": 1,
                "value": 0.9,
                "met": 1,
                "captured_at": "2026-08-14T12:01:00Z",
            }
        )
        same_instant_tree = asyncio.run(client.get(f"/api/goals/tree/{goal['id']}"))
        missing = asyncio.run(client.get("/api/goals/tree/missing"))
        absent = asyncio.run(
            client.patch(f"/api/goals/{goal['id']}/target", json={"target": _target()})
        )
        invalid = asyncio.run(
            client.patch(
                f"/api/goals/{goal['id']}/target",
                json={"target": _target()},
                headers={"X-Session-Token": "invalid"},
            )
        )
        valid = asyncio.run(
            client.patch(
                f"/api/goals/{goal['id']}/target",
                json={"target": _target(target=0.9)},
                headers={"X-Session-Token": "valid"},
            )
        )
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()

    assert tree.status_code == 200
    assert tree.json()["latest_reading"]["cycle"] == 2
    assert tree.json()["sustain_progress"] == {"consecutive_met": 2, "periods": 2}
    assert same_instant_tree.json()["sustain_progress"] == {
        "consecutive_met": 1,
        "periods": 2,
    }
    assert missing.status_code == 404
    assert absent.status_code == invalid.status_code == 403
    assert valid.status_code == 200 and valid.json()["target"]["target"] == 0.9


def test_concurrent_duplicate_creates_yield_one_row(tmp_path) -> None:
    """r3: the 409 must come from the UNIQUE constraint inside one serialized
    write — twelve concurrent creates of one name yield exactly one row, and
    no duplicate ever takes upsert_goal's update path (which would clear
    lineage or lower bars past the ancestry guard)."""
    import concurrent.futures

    from omniagentos.steward.store import GoalExistsError

    steward = StewardStore(SqliteStore(str(tmp_path / "race.db")))
    payload = {
        "name": "raced-goal",
        "north_star": {},
        "target": _target(),
        "parent_goal_id": None,
        "origin": "human",
    }

    def _create() -> str:
        try:
            return "created:" + steward.insert_goal(dict(payload))["id"]
        except GoalExistsError:
            return "refused"

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(lambda _: _create(), range(12)))

    created = [o for o in outcomes if o.startswith("created:")]
    assert len(created) == 1, outcomes
    assert outcomes.count("refused") == 11
    rows = steward.list_goals()
    named = [g for g in rows if g["name"] == "raced-goal"]
    assert len(named) == 1
    assert named[0]["origin"] == "human"
