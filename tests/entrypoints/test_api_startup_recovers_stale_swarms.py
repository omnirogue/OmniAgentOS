"""T1 — crash recovery must fire when the real API process starts.

Entry point: the real ASGI app's ``lifespan`` (``omniagentos.api:app``),
which is what ``make api`` / ``uvicorn omniagentos.api:app`` runs.

Mechanism observed WITHOUT importing it: a pre-seeded ``swarm_runs`` row
with ``status='running'`` and a heartbeat older than the 2-minute adopt
threshold must not remain a stale running orphan after startup. Recovery
is observed only via DB state (adopted heartbeat refresh, or terminal
status). This deliberately does NOT import ``resume_stale_swarms`` or
``SessionSupervisor``.

Regression contract for O-16: the API lifespan now calls the recovery sweep
directly. Heartbeat-lease takeover deliberately defaults off, so the positive
test enables ``OMNIAGENTOS_SWARM_EXECUTE`` and its companion documents the
flag-off behavior.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniagentos.collab.store import CollabStore
from omniagentos.swarm.dal import SwarmDal

# Threshold documented on resume_stale_swarms / DEFAULT_ADOPT_STALE_MINUTES.
_STALE_THRESHOLD_MINUTES = 2.0
_HEARTBEAT_AGE_MINUTES = 10.0  # well past the threshold
_DEADLINE_SECONDS = 5.0


@pytest.fixture(scope="session", autouse=True)
def _isolate_vault(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Keep the WP7 summary note a recovered run writes out of the working tree.

    ``default_vault_dir()`` falls back to ``<repo>/vault`` when
    ``OMNIAGENTOS_VAULT_DIR`` is unset, so a successfully adopted run drops a
    ``vault/swarm/<run_id>.md`` note there. That directory is tracked and not
    gitignored, so a green run dirtied the repo (~2 of 3 runs).

    Session-scoped on purpose. The note is written by the resumed coordinator's
    background thread, which outlives the test that started it — a
    function-scoped ``monkeypatch`` is undone first, so the late write lands in
    the repo anyway. That is exactly what happened when this module ran as part
    of the full ``tests/entrypoints/`` directory.
    """
    patcher = pytest.MonkeyPatch()
    patcher.setenv("OMNIAGENTOS_VAULT_DIR", str(tmp_path_factory.mktemp("vault")))
    yield
    patcher.undo()


def _seed_orphaned_running(db_path: str) -> str:
    """Precondition only: seed a running run with a stale heartbeat.

    Uses SwarmDal + raw sqlite for the heartbeat stamp (same recipe as the
    unit suite's seeding helper). Seeding is NOT the mechanism under test.
    """
    CollabStore(db_path)  # apply migrations
    dal = SwarmDal(db_path)
    try:
        run_id = str(dal.create_run(working_dir="/tmp/ws", goal="orphaned for entrypoint T1", source="test")["id"])
        assert dal.set_run_status(run_id, "running")
        stamp = (datetime.now(UTC) - timedelta(minutes=_HEARTBEAT_AGE_MINUTES)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "UPDATE swarm_runs SET heartbeat_at = ? WHERE id = ?",
                (stamp, run_id),
            )
            connection.commit()
        finally:
            connection.close()
        return run_id
    finally:
        dal.close()


def _read_run(db_path: str, run_id: str) -> dict[str, object] | None:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT id, status, heartbeat_at FROM swarm_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def _parse_heartbeat(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_recovered(row: dict[str, object] | None) -> bool:
    """True when the orphan is no longer a stale running coordinator lease."""
    if row is None:
        return True
    status = str(row.get("status") or "")
    if status in {"failed", "completed", "cancelled", "killed"}:
        return True
    if status not in {"running", "merging"}:
        # Any non-active status counts as reaped/moved.
        return True
    beat = _parse_heartbeat(row.get("heartbeat_at"))
    if beat is None:
        return False
    age_minutes = (datetime.now(UTC) - beat).total_seconds() / 60.0
    # Adopted: heartbeat refreshed to within the stale threshold.
    return age_minutes < _STALE_THRESHOLD_MINUTES


def test_api_startup_recovers_stale_swarm_run(
    campaign_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real lifespan must recover a stale running swarm row (O-16).

    (a) Entry point: ``TestClient(omniagentos.api:app)`` context manager, which
        runs the app's real ``lifespan`` startup hook (same object uvicorn loads).
    (b) Mechanism observed without importing it: DB row for the orphaned run is
        no longer a stale ``running`` lease (adopted or reaped) within a short
        poll deadline.
    """
    # Heartbeat-lease takeover is deliberately execution-gated; enable it here
    # so this test asserts the wired recovery path itself.
    monkeypatch.setenv("OMNIAGENTOS_SWARM_RESUME_ON_STARTUP", "1")
    monkeypatch.setenv("OMNIAGENTOS_SWARM_EXECUTE", "1")

    db_path = str(campaign_root / "state.sqlite3")
    run_id = _seed_orphaned_running(db_path)

    before = _read_run(db_path, run_id)
    assert before is not None
    assert before["status"] == "running"
    assert not _is_recovered(before), "precondition: seeded run must look stale"

    # Import the production app object — do not hand-assemble a FastAPI().
    from omniagentos.api import app as production_app

    # Entering the context runs the real lifespan (daemon resume thread included).
    with TestClient(production_app) as client:
        # Touch the app so the ASGI stack is live; recovery itself is startup-side.
        health = client.get("/api/health")
        # Health may or may not require auth; 200/401 both prove the app is up.
        assert health.status_code in {200, 401, 404} or health.status_code < 500

        deadline = time.monotonic() + _DEADLINE_SECONDS
        recovered = False
        while time.monotonic() < deadline:
            row = _read_run(db_path, run_id)
            if _is_recovered(row):
                recovered = True
                break
            time.sleep(0.05)

    assert recovered, (
        f"orphaned swarm run {run_id!r} still stale after real app startup "
        f"(last row={_read_run(db_path, run_id)!r}); production lifespan did not "
        f"recover it within {_DEADLINE_SECONDS}s"
    )


def test_api_startup_leaves_stale_swarm_unadopted_when_execution_defaults_off(
    campaign_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Document the deliberate default-off gate on heartbeat-lease takeover.

    The recovery wiring still runs at real app startup, but without
    ``OMNIAGENTOS_SWARM_EXECUTE`` it must not adopt a stale active run.
    """
    monkeypatch.setenv("OMNIAGENTOS_SWARM_RESUME_ON_STARTUP", "1")
    monkeypatch.delenv("OMNIAGENTOS_SWARM_EXECUTE", raising=False)

    db_path = str(campaign_root / "state.sqlite3")
    run_id = _seed_orphaned_running(db_path)

    before = _read_run(db_path, run_id)
    assert before is not None
    assert before["status"] == "running"
    assert not _is_recovered(before), "precondition: seeded run must look stale"

    # Import the production app object — do not hand-assemble a FastAPI().
    from omniagentos.api import app as production_app

    # Entering the context runs the real lifespan with the takeover flag absent.
    with TestClient(production_app) as client:
        health = client.get("/api/health")
        assert health.status_code in {200, 401, 404} or health.status_code < 500

    after = _read_run(db_path, run_id)
    assert after is not None
    assert after["status"] == "running"
    assert after["heartbeat_at"] == before["heartbeat_at"]
    heartbeat = _parse_heartbeat(after["heartbeat_at"])
    assert heartbeat is not None
    age_minutes = (datetime.now(UTC) - heartbeat).total_seconds() / 60.0
    assert age_minutes >= _STALE_THRESHOLD_MINUTES
