"""Startup routine-seed isolation tests."""

from __future__ import annotations

import asyncio
import builtins
import logging
from pathlib import Path
from typing import Any

import pytest

from omniagentos.api.main import app, lifespan
from omniagentos.db.store import SqliteStore
from tests.routines.conftest import apply_routines_meta_migration
from tests.support.db_template import make_store


def test_w3_import_failure_is_logged_without_skipping_sibling_seeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = make_store(SqliteStore, tmp_path / "startup.db")
    apply_routines_meta_migration(store)
    calls: list[str] = []

    monkeypatch.setenv("OMNIAGENTOS_ORCH_RESUME_ON_STARTUP", "0")
    monkeypatch.setenv("OMNIAGENTOS_SWARM_RESUME_ON_STARTUP", "0")
    monkeypatch.setenv("OMNIAGENTOS_SEED_ROUTINES_ON_STARTUP", "1")
    monkeypatch.setenv("OMNIAGENTOS_SEED_EMPLOYEES_ON_STARTUP", "0")
    monkeypatch.setenv("OMNIAGENTOS_INDEX_VAULT_ON_STARTUP", "0")
    monkeypatch.setattr("omniagentos.api.main._assert_explicit_control_plane_db", lambda: None)
    monkeypatch.setattr("omniagentos.api.main._assert_migration_inventory", lambda: None)
    monkeypatch.setattr("omniagentos.api.main.assert_startup_coherence", lambda: None)
    monkeypatch.setattr("omniagentos.api.main._mint_session_token_on_first_boot", lambda: None)
    monkeypatch.setattr("omniagentos.api.deps.get_store", lambda: store)
    monkeypatch.setattr("omniagentos.swarm.scheduler.shutdown_default_schedulers", lambda: None)
    monkeypatch.setattr(
        "omniagentos.improve.dispatcher.ensure_improve_dispatcher_routine",
        lambda _: calls.append("improve"),
    )
    monkeypatch.setattr(
        "omniagentos.lab.jobs.ensure_lab_jobs_routine", lambda _: calls.append("lab")
    )
    monkeypatch.setattr(
        "omniagentos.memlife.dream.ensure_dream_cycle_routine",
        lambda _: calls.append("dream"),
    )

    real_import = builtins.__import__

    def fail_loops_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "omniagentos_loops.registry":
            raise ImportError("simulated missing loops package")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_loops_import)
    with caplog.at_level(logging.ERROR):
        asyncio.run(_drive_lifespan())

    assert calls == ["improve", "lab", "dream"]
    assert "w3-health-monitor NOT seeded" in caplog.text
    assert "simulated missing loops package" in caplog.text


def test_sibling_seed_failure_does_not_skip_w3_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = make_store(SqliteStore, tmp_path / "startup2.db")
    apply_routines_meta_migration(store)
    w3_calls: list[str] = []

    monkeypatch.setenv("OMNIAGENTOS_ORCH_RESUME_ON_STARTUP", "0")
    monkeypatch.setenv("OMNIAGENTOS_SWARM_RESUME_ON_STARTUP", "0")
    monkeypatch.setenv("OMNIAGENTOS_SEED_ROUTINES_ON_STARTUP", "1")
    monkeypatch.setenv("OMNIAGENTOS_SEED_EMPLOYEES_ON_STARTUP", "0")
    monkeypatch.setenv("OMNIAGENTOS_INDEX_VAULT_ON_STARTUP", "0")
    monkeypatch.setattr("omniagentos.api.main._assert_explicit_control_plane_db", lambda: None)
    monkeypatch.setattr("omniagentos.api.main._assert_migration_inventory", lambda: None)
    monkeypatch.setattr("omniagentos.api.main.assert_startup_coherence", lambda: None)
    monkeypatch.setattr("omniagentos.api.main._mint_session_token_on_first_boot", lambda: None)
    monkeypatch.setattr("omniagentos.api.deps.get_store", lambda: store)
    monkeypatch.setattr("omniagentos.swarm.scheduler.shutdown_default_schedulers", lambda: None)

    def fail_sibling(_: Any) -> None:
        raise RuntimeError("simulated sibling seed failure")

    monkeypatch.setattr(
        "omniagentos.improve.dispatcher.ensure_improve_dispatcher_routine", fail_sibling
    )
    monkeypatch.setattr(
        "omniagentos.scheduler.loop_seeding.ensure_w3_health_monitor_routine",
        lambda _: w3_calls.append("w3"),
    )

    with caplog.at_level(logging.ERROR):
        asyncio.run(_drive_lifespan())

    assert "Startup routine seed failed" in caplog.text
    assert "simulated sibling seed failure" in caplog.text
    assert w3_calls == ["w3"]


async def _drive_lifespan() -> None:
    async with lifespan(app):
        pass
