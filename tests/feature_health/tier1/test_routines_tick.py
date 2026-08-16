"""Feature-health tier1 — routines tick + enable-activation gate ($0, mock only).

(a) The real CLI: ``python -m omniagentos.scheduler.routines_tick --dry-run`` as a
    subprocess with ``env=fh_subprocess_env`` (DB pin redirected to a seeded tmp
    ``OMNIAGENTOS_DB``) — the JSON summary must list the enabled routine as due
    and the disabled one as skipped with a reason, writing nothing.
(b) Non-dry in-process tick with a ``* * * * *`` cron routine on the MOCK harness
    (never spends): exactly one fire recorded (``last_fired``/``total_runs``),
    and a second tick at the same instant does NOT double-fire.
(c) D5 enable-activation gate (seam: ``omniagentos.api.routes.routines
    ._pytest_counts_prove_life`` via ``_run_activation_gate``): a pytest gate
    with ZERO passed tests (all-skipped/empty proof) is REFUSED and the row
    stays disabled; a real 1-test target activates. Idioms from
    ``tests/routines/test_tick.py`` and ``tests/api/test_routine_activation.py``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.routines import _pytest_counts_prove_life
from omniagentos.db.store import SqliteStore
from omniagentos.policy import PolicyConfig, load_policy
from omniagentos.scheduler.routines_tick import tick
from omniagentos.scheduler.store import RoutinesStore
from tests.routines.conftest import apply_routines_meta_migration, valid_routine_payload

_REPO_ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)  # "* * * * *" matches any minute


@pytest.fixture()
def database(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(str(tmp_path / "fh-routines.db"))
    apply_routines_meta_migration(store)
    return store


@pytest.fixture()
def routines(database: SqliteStore) -> RoutinesStore:
    return RoutinesStore(database)


@pytest.fixture()
def policy() -> PolicyConfig:
    return load_policy()


def _mock_cron_routine(routines: RoutinesStore, **overrides: Any) -> dict[str, Any]:
    payload = valid_routine_payload(
        trigger_config={"cron": "* * * * *"},
        task_template={"title": "FH routine-fired task", "harness": "mock"},
    )
    payload.update(overrides)
    return routines.create_routine(payload)


def test_dry_run_subprocess_lists_due_and_skipped_without_writing(
    tmp_path: Path,
    database: SqliteStore,
    routines: RoutinesStore,
    fh_subprocess_env: dict[str, str],
) -> None:
    enabled = _mock_cron_routine(routines, name="fh-dry-run-due")
    disabled = _mock_cron_routine(routines, name="fh-dry-run-disabled", status="disabled")

    db_row = database._connection.execute("PRAGMA database_list").fetchone()
    db_path = str(db_row[2])
    env = dict(fh_subprocess_env)
    env["OMNIAGENTOS_DB"] = db_path  # seeded tmp DB, still under pytest tmp isolation

    completed = subprocess.run(
        [sys.executable, "-m", "omniagentos.scheduler.routines_tick", "--dry-run"],
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    json_lines = [
        line for line in completed.stdout.splitlines() if line.strip().startswith("{")
    ]
    assert json_lines, f"no JSON summary on stdout: {completed.stdout!r}"
    summary = json.loads(json_lines[-1])

    assert summary["dry_run"] is True
    fired_ids = {entry["routine_id"] for entry in summary["fired"]}
    assert enabled["id"] in fired_ids
    due_entry = next(e for e in summary["fired"] if e["routine_id"] == enabled["id"])
    assert due_entry["fired"] is False and due_entry["dry_run"] is True
    skipped_entry = next(
        e for e in summary["skipped"] if e["routine_id"] == disabled["id"]
    )
    assert skipped_entry["reason"], "skipped entry must carry a reason"
    assert "not active" in skipped_entry["reason"]

    # Dry run wrote NOTHING: no runs, no last_fired.
    fresh = routines.get_routine(enabled["id"])
    assert fresh["total_runs"] == 0
    assert fresh["last_fired"] is None
    assert routines.list_runs(enabled["id"]) == []


def test_mock_harness_fires_exactly_once_and_never_double_fires(
    database: SqliteStore, routines: RoutinesStore, policy: PolicyConfig
) -> None:
    created = _mock_cron_routine(routines, name="fh-single-fire")

    result = tick(database, policy, now=NOW)
    assert len(result["fired"]) == 1
    entry = result["fired"][0]
    assert entry["routine_id"] == created["id"]
    assert entry["fired"] is True
    assert entry["task_id"] and entry["run_id"]

    run = database.get_run(entry["run_id"])
    assert run is not None and run["state"] == "queued"

    fired_row = routines.get_routine(created["id"])
    assert fired_row["total_runs"] == 1
    assert fired_row["last_fired"] is not None
    runs = routines.list_runs(created["id"])
    assert len(runs) == 1 and runs[0]["run_id"] == entry["run_id"]

    # Second immediate tick (same instant): must NOT double-fire.
    second = tick(database, policy, now=NOW)
    assert second["fired"] == []
    reason = next(
        s["reason"] for s in second["skipped"] if s["routine_id"] == created["id"]
    )
    assert "cron trigger not due" in reason
    assert routines.get_routine(created["id"])["total_runs"] == 1
    assert len(routines.list_runs(created["id"])) == 1


# --- (c) D5 enable-activation gate -----------------------------------------

# 1 collected / 1 skipped / 0 passed — exits 0 but proves no life.
_NO_LIFE_GATE_CMD = "pytest tests/api/activation_skip_gate_probe.py"
# Exactly one real, fast, DB-free test — collected=1 passed=1.
_ONE_TEST_GATE_CMD = (
    "pytest tests/connectors/test_jira_retry.py::test_non_retryable_create_and_comment_paths"
)


def _activatable(**overrides: Any) -> dict[str, Any]:
    payload = valid_routine_payload(
        status="disabled",
        gate_type="exit_code",
        task_template={"title": "real work", "harness": "cli-grok"},
    )
    payload.update(overrides)
    return payload


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_prove_life_seam_rejects_empty_and_zero_passed_suites() -> None:
    """The exact activation seam: collect-only/empty and 0-passed both refuse."""
    ok, detail = _pytest_counts_prove_life(0, 0, 0, 0, 0)
    assert ok is False and "zero tests collected" in detail
    ok, detail = _pytest_counts_prove_life(2, 0, 0, 0, 2)
    assert ok is False and "all-skipped" in detail
    ok, detail = _pytest_counts_prove_life(1, 0, 0, 0, 0)
    assert ok is False and "passed=0" in detail
    ok, detail = _pytest_counts_prove_life(1, 1, 0, 0, 0)
    assert ok is True and "passed=1" in detail


def test_enable_refused_when_gate_target_proves_no_life(database: SqliteStore) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = _activatable(
                    name="fh-no-life-gate",
                    gate_config={"command": _NO_LIFE_GATE_CMD, "expected_exit_code": 0},
                )
                created = (await client.post("/api/routines", json=payload)).json()
                assert created["status"] == "disabled"
                enable = await client.post(f"/api/routines/{created['id']}/enable")
                assert enable.status_code == 400, enable.text
                blob = str(enable.json()).lower()
                assert "skip" in blob or "passed" in blob or "collected" in blob
                fetched = (await client.get(f"/api/routines/{created['id']}")).json()
                assert fetched["status"] == "disabled"
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_enable_passes_with_real_one_test_gate_target(database: SqliteStore) -> None:
    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: database
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = _activatable(
                    name="fh-one-test-gate",
                    gate_config={"command": _ONE_TEST_GATE_CMD, "expected_exit_code": 0},
                )
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
