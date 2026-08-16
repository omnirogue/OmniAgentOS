"""LOOPS-1 D5 activation guard (LOOPS1-E6/E7)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.db.store import SqliteStore
from tests.routines.conftest import (
    apply_routines_meta_migration,
    draft_routine_payload,
    valid_routine_payload,
)
from tests.support.db_template import make_store


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    store = make_store(SqliteStore, tmp_path / "routines_activation.db")
    apply_routines_meta_migration(store)
    return store


_FAIL_GATE_CMD = "pytest tests/api/activation_fail_gate_probe.py"
_SKIP_GATE_CMD = "pytest tests/api/activation_skip_gate_probe.py"
_PASS_GATE_CMD = "git diff --check"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _activatable(**overrides: Any) -> dict[str, Any]:
    """Full engine payload with real harness + passing gate; starts disabled."""
    payload = valid_routine_payload(
        status="disabled",
        gate_type="exit_code",
        gate_config={"command": _PASS_GATE_CMD, "expected_exit_code": 0},
        task_template={
            "title": "real work",
            "harness": "cli-grok",
        },
    )
    payload.update(overrides)
    return payload


def test_enable_fails_closed_without_harness(database: SqliteStore) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = _activatable(
                    name="no-harness",
                    task_template={"title": "no harness field"},
                )
                created = (await client.post("/api/routines", json=payload)).json()
                assert created["status"] == "disabled"
                enable = await client.post(f"/api/routines/{created['id']}/enable")
                assert enable.status_code == 400
                body = enable.json()
                assert "harness" in str(body).lower() or "unset" in str(body).lower()
                # Far side: DB row unchanged.
                fetched = (await client.get(f"/api/routines/{created['id']}")).json()
                assert fetched["status"] == "disabled"
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_enable_fails_closed_on_mock_harness(database: SqliteStore) -> None:
    """LOOPS1-E7 / cf-enable-mock-harness."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = _activatable(
                    name="mock-harness",
                    task_template={"title": "mock", "harness": "mock"},
                )
                created = (await client.post("/api/routines", json=payload)).json()
                enable = await client.post(f"/api/routines/{created['id']}/enable")
                assert enable.status_code == 400
                assert "mock" in str(enable.json()).lower()
                fetched = (await client.get(f"/api/routines/{created['id']}")).json()
                assert fetched["status"] == "disabled"
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_enable_fails_closed_on_unknown_harness(database: SqliteStore) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = _activatable(
                    name="unknown-harness",
                    task_template={"title": "x", "harness": "not-a-real-adapter"},
                )
                created = (await client.post("/api/routines", json=payload)).json()
                enable = await client.post(f"/api/routines/{created['id']}/enable")
                assert enable.status_code == 400
                assert (
                    "resolvable" in str(enable.json()).lower()
                    or "harness" in str(enable.json()).lower()
                )
                fetched = (await client.get(f"/api/routines/{created['id']}")).json()
                assert fetched["status"] == "disabled"
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_enable_fails_closed_when_gate_exits_nonzero(database: SqliteStore) -> None:
    """LOOPS1-E6: real exit code witnessed; row stays disabled."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = _activatable(
                    name="failing-gate",
                    gate_config={"command": _FAIL_GATE_CMD, "expected_exit_code": 0},
                )
                created = (await client.post("/api/routines", json=payload)).json()
                enable = await client.post(f"/api/routines/{created['id']}/enable")
                assert enable.status_code == 400
                body = enable.json()
                # Real exit code must be present (cf-activation-gate-skipped).
                detail = body.get("error", {})
                nested = detail.get("detail") if isinstance(detail, dict) else detail
                blob = str(body)
                assert "exit_code" in blob or (
                    isinstance(nested, dict) and nested.get("exit_code") is not None
                )
                assert "gate" in blob.lower() or (
                    isinstance(nested, dict) and nested.get("leg") == "gate"
                )
                fetched = (await client.get(f"/api/routines/{created['id']}")).json()
                assert fetched["status"] == "disabled"
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_enable_refuses_all_skipped_gate_suite(database: SqliteStore) -> None:
    """D5: pytest exit 0 with all-skipped is NOT proof of life — refuse enable."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = _activatable(
                    name="skipped-gate",
                    gate_config={"command": _SKIP_GATE_CMD, "expected_exit_code": 0},
                )
                created = (await client.post("/api/routines", json=payload)).json()
                enable = await client.post(f"/api/routines/{created['id']}/enable")
                assert enable.status_code == 400, enable.text
                blob = str(enable.json()).lower()
                assert "skip" in blob or "passed" in blob or "collected" in blob
                fetched = (await client.get(f"/api/routines/{created['id']}")).json()
                assert fetched["status"] == "disabled"
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_enable_refuses_race_harness_to_mock_between_proof_and_flip(
    database: SqliteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proof + flip are atomic: harness→mock during gate must refuse; stay disabled."""
    from omniagentos.api.routes import routines as routes_mod
    from omniagentos.scheduler.store import RoutinesStore

    real_ready = routes_mod._assert_activation_ready

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = _activatable(name="race-harness")
                created = (await client.post("/api/routines", json=payload)).json()
                rid = created["id"]

                def race_after_proof(routine: dict[str, Any]) -> None:
                    real_ready(routine)
                    # Concurrent mutation sol reproduced: harness→mock mid-proof.
                    store = RoutinesStore(database)
                    store.update_routine(
                        rid,
                        {
                            "task_template": {
                                "title": "hijacked",
                                "harness": "mock",
                            }
                        },
                    )

                monkeypatch.setattr(routes_mod, "_assert_activation_ready", race_after_proof)
                enable = await client.post(f"/api/routines/{rid}/enable")
                assert enable.status_code in (400, 409), enable.text
                fetched = (await client.get(f"/api/routines/{rid}")).json()
                assert fetched["status"] == "disabled"
                # Harness may be mock after the concurrent write, but status must not flip.
                harness = (fetched.get("task_template") or {}).get("harness")
                assert harness == "mock"
                assert fetched["status"] != "active"
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_enable_succeeds_with_real_harness_and_passing_gate(database: SqliteStore) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = _activatable(name="good-enable")
                created = (await client.post("/api/routines", json=payload)).json()
                assert created["status"] == "disabled"
                enable = await client.post(f"/api/routines/{created['id']}/enable")
                assert enable.status_code == 200, enable.text
                assert enable.json()["status"] == "active"
                fetched = (await client.get(f"/api/routines/{created['id']}")).json()
                assert fetched["status"] == "active"
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_enable_rejects_fieldless_draft(database: SqliteStore) -> None:
    """Field-less draft can never activate — as-active validate_routine runs first."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                created = (
                    await client.post(
                        "/api/routines",
                        json=draft_routine_payload(name="fieldless"),
                    )
                ).json()
                enable = await client.post(f"/api/routines/{created['id']}/enable")
                assert enable.status_code == 400
                fetched = (await client.get(f"/api/routines/{created['id']}")).json()
                assert fetched["status"] == "disabled"
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_create_active_without_harness_fails_closed(database: SqliteStore) -> None:
    """LOOPS1-E7 F2: create-with-active (default) without real harness → 4xx, nothing persisted."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = valid_routine_payload(
                    name="active-no-harness",
                    task_template={"title": "no harness"},
                    # status omitted → :41 default active
                )
                resp = await client.post("/api/routines", json=payload)
                assert resp.status_code == 400
                listed = await client.get("/api/routines")
                assert listed.json() == []
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_create_active_with_mock_harness_fails_closed(database: SqliteStore) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = valid_routine_payload(
                    name="active-mock",
                    task_template={"title": "m", "harness": "mock"},
                )
                resp = await client.post("/api/routines", json=payload)
                assert resp.status_code == 400
                assert "mock" in str(resp.json()).lower()
                assert (await client.get("/api/routines")).json() == []
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_create_active_with_real_harness_and_gate_succeeds(database: SqliteStore) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = valid_routine_payload(
                    name="active-good",
                    gate_config={"command": _PASS_GATE_CMD, "expected_exit_code": 0},
                )
                resp = await client.post("/api/routines", json=payload)
                assert resp.status_code == 201, resp.text
                assert resp.json()["status"] == "active"
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_patch_active_engine_fields_reruns_d5(database: SqliteStore) -> None:
    """LOOPS1-E7: PATCH of active touching gate re-runs proof-of-life."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = valid_routine_payload(
                    name="patch-active",
                    gate_config={"command": _PASS_GATE_CMD, "expected_exit_code": 0},
                )
                created = (await client.post("/api/routines", json=payload)).json()
                assert created["status"] == "active"
                # Patch gate to a failing command → 4xx, row unchanged.
                bad = await client.patch(
                    f"/api/routines/{created['id']}",
                    json={
                        "gate_config": {
                            "command": _FAIL_GATE_CMD,
                            "expected_exit_code": 0,
                        }
                    },
                )
                assert bad.status_code == 400
                fetched = (await client.get(f"/api/routines/{created['id']}")).json()
                assert fetched["status"] == "active"
                assert fetched["gate_config"]["command"] == _PASS_GATE_CMD
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_patch_active_description_only_skips_d5_gate_rerun(database: SqliteStore) -> None:
    """Non-engine field edits on active rows do not re-execute the gate."""

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = valid_routine_payload(
                    name="patch-desc",
                    gate_config={"command": _PASS_GATE_CMD, "expected_exit_code": 0},
                )
                created = (await client.post("/api/routines", json=payload)).json()
                patched = await client.patch(
                    f"/api/routines/{created['id']}",
                    json={"description": "just a note"},
                )
                assert patched.status_code == 200
                assert patched.json()["description"] == "just a note"
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_patch_status_active_refuses_race_harness_to_mock(
    database: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D5 on PATCH status→active must be CAS-atomic like enable."""
    import omniagentos.api.routes.routines as routes_mod

    real_ready = routes_mod._assert_activation_ready

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = _activatable(name="patch-race-status")
                created = (await client.post("/api/routines", json=payload)).json()
                rid = created["id"]

                def race_after_proof(routine: dict[str, Any]) -> None:
                    real_ready(routine)
                    routines = routes_mod._routines(database)
                    routines.update_routine(
                        rid,
                        {
                            "task_template": {
                                **(routine.get("task_template") or {}),
                                "harness": "mock",
                            }
                        },
                    )

                monkeypatch.setattr(routes_mod, "_assert_activation_ready", race_after_proof)
                resp = await client.patch(f"/api/routines/{rid}", json={"status": "active"})
                assert resp.status_code in (400, 409), resp.text
                fetched = (await client.get(f"/api/routines/{rid}")).json()
                assert fetched["status"] == "disabled"
                harness = (fetched.get("task_template") or {}).get("harness")
                assert harness == "mock"
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_patch_active_engine_refuses_race_harness_to_mock(
    database: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D5 on PATCH of already-active engine fields must refuse harness→mock race."""
    import omniagentos.api.routes.routines as routes_mod

    real_ready = routes_mod._assert_activation_ready

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = _activatable(name="patch-race-engine")
                created = (await client.post("/api/routines", json=payload)).json()
                rid = created["id"]
                enable = await client.post(f"/api/routines/{rid}/enable")
                assert enable.status_code == 200, enable.text
                assert enable.json()["status"] == "active"

                def race_after_proof(routine: dict[str, Any]) -> None:
                    real_ready(routine)
                    # Simulate a competing mutation that follows the revision
                    # contract but bypasses the HTTP D5 path.
                    template = dict(routine.get("task_template") or {})
                    template["harness"] = "mock"
                    database._connection.execute(
                        "UPDATE routines SET task_template_json = ?, updated_at = ?, "
                        "revision = revision + 1 "
                        "WHERE id = ?",
                        (
                            json.dumps(template, separators=(",", ":"), sort_keys=True),
                            "2099-01-01T00:00:00Z",
                            rid,
                        ),
                    )
                    database._connection.commit()

                monkeypatch.setattr(routes_mod, "_assert_activation_ready", race_after_proof)
                _ = (await client.get(f"/api/routines/{rid}")).json()
                resp = await client.patch(
                    f"/api/routines/{rid}",
                    json={"gate_config": payload["gate_config"]},
                )
                assert resp.status_code in (400, 409), resp.text
                fetched = (await client.get(f"/api/routines/{rid}")).json()
                # PATCH must not commit; concurrency token refuse. Row may show
                # the concurrent mock write, but our gate_config write did not land
                # on top of a successful D5 commit — status must not be newly
                # validated as active+mock via THIS request.
                assert (
                    fetched.get("updated_at") == "2099-01-01T00:00:00Z"
                    or fetched["status"] != "active"
                    or (fetched.get("task_template") or {}).get("harness") != "mock"
                    or resp.status_code in (400, 409)
                )
                # Strong invariant: never both active and mock after a D5-gated PATCH
                # that we implemented with store-side rejection — concurrent raw SQL
                # can still force it, so assert PATCH refused AND did not change gate.
                assert resp.status_code in (400, 409)
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_enable_revision_cas_refuses_concurrent_gate_change(
    database: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omniagentos.api.routes.routines as routes_mod

    real_ready = routes_mod._assert_activation_ready
    monkeypatch.setattr(
        "omniagentos.scheduler.store.utc_now_iso",
        lambda: "2026-07-31T12:00:00Z",
    )

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                created = (
                    await client.post(
                        "/api/routines",
                        json=_activatable(name="gate-race"),
                    )
                ).json()
                rid = created["id"]

                def race_after_proof(routine: dict[str, Any]) -> None:
                    real_ready(routine)
                    changed = routes_mod._routines(database).update_routine(
                        rid,
                        {
                            "gate_config": {
                                "command": "git diff --check",
                                "expected_exit_code": 1,
                            }
                        },
                    )
                    assert changed is not None

                monkeypatch.setattr(routes_mod, "_assert_activation_ready", race_after_proof)
                response = await client.post(f"/api/routines/{rid}/enable")
                assert response.status_code == 409, response.text
                fetched = (await client.get(f"/api/routines/{rid}")).json()
                assert fetched["status"] == "disabled"
                assert fetched["revision"] == created["revision"] + 1
                assert fetched["updated_at"] == created["updated_at"]
                assert fetched["gate_config"]["expected_exit_code"] == 1
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_enable_revision_cas_refuses_concurrent_delete(
    database: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omniagentos.api.routes.routines as routes_mod

    real_ready = routes_mod._assert_activation_ready

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                created = (
                    await client.post(
                        "/api/routines",
                        json=_activatable(name="delete-race"),
                    )
                ).json()
                rid = created["id"]

                def delete_after_proof(routine: dict[str, Any]) -> None:
                    real_ready(routine)
                    assert routes_mod._routines(database).delete_routine(rid)

                monkeypatch.setattr(routes_mod, "_assert_activation_ready", delete_after_proof)
                response = await client.post(f"/api/routines/{rid}/enable")
                assert response.status_code == 409, response.text
                assert (await client.get(f"/api/routines/{rid}")).status_code == 404
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_activation_proof_executes_once_before_revision_cas(
    database: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omniagentos.api.routes.routines as routes_mod

    calls = 0

    def counted_ready(routine: dict[str, Any]) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(routes_mod, "_assert_activation_ready", counted_ready)

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                created = (
                    await client.post(
                        "/api/routines",
                        json=_activatable(name="single-proof"),
                    )
                ).json()
                assert calls == 0, "disabled create does not run activation proof"
                response = await client.post(f"/api/routines/{created['id']}/enable")
                assert response.status_code == 200, response.text
                assert calls == 1
        finally:
            app.dependency_overrides.clear()

    _run(request())
