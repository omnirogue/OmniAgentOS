"""Regression coverage for the W3 routine seed and its import-light registry."""

from __future__ import annotations

from pathlib import Path

from omniagentos.db.store import SqliteStore
from omniagentos.scheduler.loop_seeding import ensure_w3_health_monitor_routine
from omniagentos.scheduler.routines import validate_routine
from omniagentos.scheduler.store import RoutinesStore
from tests.routines.conftest import apply_routines_meta_migration
from tests.support.db_template import make_store


def test_w3_seed_creates_a_valid_loop_row(tmp_path: Path) -> None:
    store = make_store(SqliteStore, tmp_path / "w3.db")
    apply_routines_meta_migration(store)

    row = ensure_w3_health_monitor_routine(store)

    assert row["name"] == "w3-health-monitor"
    validate_routine(row)
    assert any(r["name"] == "w3-health-monitor" for r in RoutinesStore(store).list_routines())
