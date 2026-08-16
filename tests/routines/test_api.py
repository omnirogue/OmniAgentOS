from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.db.store import SqliteStore
from omniagentos.scheduler.routines import should_fire
from tests.routines.conftest import valid_routine_payload


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_create_list_get_routine(database: SqliteStore) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                # Active create runs D5; payload carries real harness + passing gate.
                created = await client.post("/api/routines", json=valid_routine_payload())
                assert created.status_code == 201, created.text
                body = created.json()
                assert body["id"].startswith("rtn_")
                assert body["status"] == "active"
                assert "next_run" in body

                listed = await client.get("/api/routines")
                assert listed.status_code == 200
                assert len(listed.json()) == 1

                fetched = await client.get(f"/api/routines/{body['id']}")
                assert fetched.status_code == 200
                assert fetched.json()["name"] == "nightly-lint-fix"

                missing = await client.get("/api/routines/rtn_nope")
                assert missing.status_code == 404
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_create_rejects_routine_missing_gate_or_hard_cap(database: SqliteStore) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = valid_routine_payload()
                del payload["gate_type"]
                missing_gate = await client.post("/api/routines", json=payload)
                assert missing_gate.status_code == 400
                # D5 activation_validation or store validation — either names the field.
                blob = str(missing_gate.json())
                assert "gate" in blob

                payload2 = valid_routine_payload()
                del payload2["hard_cap_type"]
                missing_cap = await client.post("/api/routines", json=payload2)
                assert missing_cap.status_code == 400
                assert "hard_cap" in str(missing_cap.json())

                assert (await client.get("/api/routines")).json() == []
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_create_disabled_draft_omitting_engine_fields(database: SqliteStore) -> None:
    """LOOPS1-E2 API draft leg."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/routines",
                    json={"name": "api-draft", "status": "disabled"},
                )
                assert resp.status_code == 201, resp.text
                assert resp.json()["status"] == "disabled"
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_update_enable_disable_delete(database: SqliteStore) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                created = (await client.post("/api/routines", json=valid_routine_payload())).json()
                routine_id = created["id"]

                patched = await client.patch(
                    f"/api/routines/{routine_id}", json={"description": "new desc"}
                )
                assert patched.status_code == 200
                assert patched.json()["description"] == "new desc"

                bad_patch = await client.patch(
                    f"/api/routines/{routine_id}", json={"gate_type": "nonsense"}
                )
                assert bad_patch.status_code == 400

                disabled = await client.post(f"/api/routines/{routine_id}/disable")
                assert disabled.status_code == 200
                assert disabled.json()["status"] == "disabled"

                # Enable re-runs D5 proof-of-life (real harness + passing gate).
                enabled = await client.post(f"/api/routines/{routine_id}/enable")
                assert enabled.status_code == 200, enabled.text
                assert enabled.json()["status"] == "active"

                deleted = await client.delete(f"/api/routines/{routine_id}")
                assert deleted.status_code == 204

                gone = await client.get(f"/api/routines/{routine_id}")
                assert gone.status_code == 404

                missing_delete = await client.delete(f"/api/routines/{routine_id}")
                assert missing_delete.status_code == 404
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_record_runs_and_auto_pause_via_api(database: SqliteStore) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                created = (await client.post("/api/routines", json=valid_routine_payload())).json()
                routine_id = created["id"]

                # Production shape: a run only counts toward the acceptance
                # floor once it carries a gate verdict AND a finish stamp.
                for i, accepted in enumerate([True, False, False], start=1):
                    resp = await client.post(
                        f"/api/routines/{routine_id}/runs",
                        json={
                            "iteration": i,
                            "gate_passed": accepted,
                            "accepted": accepted,
                            "cost_usd": 1.0,
                            "stop_reason": "gate_passed" if accepted else "gate_failed",
                            "finished_at": f"2026-01-01T09:0{i}:00Z",
                        },
                    )
                    assert resp.status_code == 201

                runs = await client.get(f"/api/routines/{routine_id}/runs")
                assert runs.status_code == 200
                assert len(runs.json()) == 3

                routine = (await client.get(f"/api/routines/{routine_id}")).json()
                assert routine["status"] == "auto_paused"
                assert routine["acceptance_rate"] < 0.5

                missing_runs = await client.get("/api/routines/rtn_missing/runs")
                assert missing_runs.status_code == 404
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_record_run_via_api_omitting_cost_usd_reads_as_unknown_and_fails_the_budget_cap_closed(
    database: SqliteStore,
) -> None:
    """Sol review, seam 1: POST /routines/{id}/runs used to default an
    omitted cost_usd to 0.0 — a KNOWN, exact free run — which meant a
    budget_usd-capped routine with unreported costs could fire forever past
    its cap. The request model no longer defaults it; the rollup goes
    UNKNOWN end to end, and the budget cap fails closed on it."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                created = (
                    await client.post(
                        "/api/routines",
                        json=valid_routine_payload(hard_cap_type="budget_usd", hard_cap_value=10.0),
                    )
                ).json()
                routine_id = created["id"]

                resp = await client.post(
                    f"/api/routines/{routine_id}/runs",
                    json={
                        "iteration": 1,
                        "gate_passed": True,
                        "accepted": True,
                        "stop_reason": "gate_passed",
                        "finished_at": "2026-01-01T09:01:00Z",
                        # cost_usd deliberately omitted — the exact production
                        # shape a caller that genuinely doesn't know the cost sends.
                    },
                )
                assert resp.status_code == 201, resp.text
                assert resp.json()["cost_usd"] is None

                routine = (await client.get(f"/api/routines/{routine_id}")).json()
                assert routine["total_cost_usd"] is None
                assert routine["cost_per_accepted_change"] is None

                runs = (await client.get(f"/api/routines/{routine_id}/runs")).json()
                assert runs[0]["cost_usd"] is None

                # End to end: the budget_usd hard cap must fail CLOSED on the
                # unknown rollup, never read it as "$0 spent, keep firing".
                fire, reason = should_fire(routine, now=datetime(2026, 1, 1, 9, 5, tzinfo=UTC))
                assert fire is False
                assert "unknown" in reason.lower()
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_record_run_via_api_with_explicit_cost_usd_still_computes_a_known_total(
    database: SqliteStore,
) -> None:
    """Regression guard: an explicit cost_usd (including 0.0) is unaffected —
    only an OMITTED cost reads as unknown."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                created = (await client.post("/api/routines", json=valid_routine_payload())).json()
                routine_id = created["id"]

                resp = await client.post(
                    f"/api/routines/{routine_id}/runs",
                    json={
                        "iteration": 1,
                        "gate_passed": True,
                        "accepted": True,
                        "cost_usd": 0.0,
                        "stop_reason": "gate_passed",
                        "finished_at": "2026-01-01T09:01:00Z",
                    },
                )
                assert resp.status_code == 201, resp.text
                assert resp.json()["cost_usd"] == 0.0

                routine = (await client.get(f"/api/routines/{routine_id}")).json()
                assert routine["total_cost_usd"] == 0.0
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_list_recent_runs_aggregate_endpoint(database: SqliteStore) -> None:
    """``GET /api/routines/runs`` must return the pinned cross-routine
    aggregate shape (section B): ``{runs: [{routine_id, routine_name, run_id,
    gate_passed, accepted, cost_usd, finished_at}]}``."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                a = (
                    await client.post("/api/routines", json=valid_routine_payload(name="a"))
                ).json()
                b = (
                    await client.post(
                        "/api/routines",
                        json=valid_routine_payload(name="b", trigger_config={"cron": "0 4 * * *"}),
                    )
                ).json()
                await client.post(
                    f"/api/routines/{a['id']}/runs",
                    json={
                        "iteration": 1,
                        "gate_passed": True,
                        "accepted": True,
                        "cost_usd": 0.5,
                        "run_id": "run_a1",
                        "finished_at": "2025-01-01T10:00:00Z",
                    },
                )
                await client.post(
                    f"/api/routines/{b['id']}/runs",
                    json={
                        "iteration": 1,
                        "gate_passed": False,
                        "accepted": False,
                        "cost_usd": 1.5,
                        "run_id": "run_b1",
                        "finished_at": "2025-01-02T10:00:00Z",
                    },
                )

                resp = await client.get("/api/routines/runs")
                assert resp.status_code == 200
                body = resp.json()
                assert "runs" in body
                assert len(body["runs"]) == 2
                first = body["runs"][0]
                assert set(first.keys()) == {
                    "routine_id",
                    "routine_name",
                    "run_id",
                    "gate_passed",
                    "accepted",
                    "cost_usd",
                    "finished_at",
                }
                # Newest first — b's run was inserted second.
                assert first["routine_name"] == "b"
                assert first["accepted"] is False
                assert body["runs"][1]["routine_name"] == "a"
                assert body["runs"][1]["accepted"] is True

                limited = await client.get("/api/routines/runs?limit=1")
                assert limited.status_code == 200
                assert len(limited.json()["runs"]) == 1
        finally:
            app.dependency_overrides.clear()

    _run(request())
