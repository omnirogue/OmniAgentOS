from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import BudgetSpec
from omniagentos.lab.contracts import Budgets, GenomeRole, GenomeSpec, GenomeStage
from omniagentos.lab.executor import execute_genome


@pytest.fixture
def simulation_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "test_profile:\n"
        "  budget:\n"
        "    max_usd_per_run: 3.0\n"
        "    max_usd_per_campaign: 4.0\n"
        "    on_exceeded: refuse\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE", "1")
    monkeypatch.setenv("OMNIAGENTOS_TEST_PROFILE_CONFIG", str(profile))
    monkeypatch.setenv("OMNIAGENTOS_SIMULATION_CAMPAIGN_ID", "campaign-test")
    monkeypatch.setenv("OMNIAGENTOS_SIMULATION_BUDGET_DB", str(tmp_path / "reservations.db"))


def _complete(cost: float) -> Budgets:
    return Budgets(
        wall_minutes=5,
        tokens=10_000,
        cost_usd=cost,
        replicates=1,
    )


def test_pre_call_reservations_enforce_run_and_campaign_caps(simulation_env: None) -> None:
    from omniagentos.budget.simulation import SimulationBudgetError, reserve_live_simulation

    first = reserve_live_simulation(
        _complete(2.0),
        BudgetSpec(cost_usd_max=2.0),
        "run-one",
        dry_run=False,
    )
    assert first is not None

    with pytest.raises(SimulationBudgetError, match="above \\$4.00 cap"):
        reserve_live_simulation(
            _complete(2.5),
            BudgetSpec(cost_usd_max=2.5),
            "run-two",
            dry_run=False,
        )
    with pytest.raises(SimulationBudgetError, match="exceeds \\$3.00 cap"):
        reserve_live_simulation(
            _complete(3.1),
            BudgetSpec(cost_usd_max=3.1),
            "run-three",
            dry_run=False,
        )


def test_overspend_opens_a_campaign_circuit_breaker(simulation_env: None) -> None:
    from omniagentos.budget.simulation import SimulationBudgetError, reserve_live_simulation

    reservation = reserve_live_simulation(
        _complete(1.0),
        BudgetSpec(cost_usd_max=1.0),
        "run-one",
        dry_run=False,
    )
    assert reservation is not None
    with pytest.raises(SimulationBudgetError, match="circuit opened"):
        reservation.record_spend(1.01)
    with pytest.raises(SimulationBudgetError, match="circuit is open"):
        reserve_live_simulation(
            _complete(1.0),
            BudgetSpec(cost_usd_max=1.0),
            "run-two",
            dry_run=False,
        )


def test_reservation_lifecycle_releases_capacity(simulation_env: None) -> None:
    from omniagentos.budget.simulation import (
        SimulationBudgetError,
        reserve_live_simulation,
    )

    settled = reserve_live_simulation(
        _complete(3.0),
        BudgetSpec(cost_usd_max=3.0),
        "run-settled",
        dry_run=False,
    )
    assert settled is not None
    settled_state = settled.settle(0.5)
    assert settled_state.state == "settled"
    assert settled_state.actual_usd == 0.5
    assert settled.settle(0.5) == settled_state

    released = reserve_live_simulation(
        _complete(3.0),
        BudgetSpec(cost_usd_max=3.0),
        "run-released",
        dry_run=False,
    )
    assert released is not None
    assert released.release().state == "released"
    assert released.release().state == "released"

    expired = reserve_live_simulation(
        _complete(3.0),
        BudgetSpec(cost_usd_max=3.0),
        "run-expired",
        dry_run=False,
    )
    assert expired is not None
    assert expired.expire().state == "expired"

    replacement = reserve_live_simulation(
        _complete(3.0),
        BudgetSpec(cost_usd_max=3.0),
        "run-replacement",
        dry_run=False,
    )
    assert replacement is not None
    with pytest.raises(SimulationBudgetError, match="above \\$4.00 cap"):
        reserve_live_simulation(
            _complete(0.6),
            BudgetSpec(cost_usd_max=0.6),
            "run-over-settled-plus-active",
            dry_run=False,
        )


def test_existing_reservation_database_upgrades_additively(
    simulation_env: None,
    tmp_path: Path,
) -> None:
    from omniagentos.budget.simulation import reserve_live_simulation

    database = tmp_path / "reservations.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE simulation_campaigns (
                campaign_id TEXT PRIMARY KEY,
                circuit_open INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE simulation_reservations (
                run_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL REFERENCES simulation_campaigns(campaign_id),
                reserved_usd REAL NOT NULL CHECK (reserved_usd > 0),
                spent_usd REAL NOT NULL DEFAULT 0 CHECK (spent_usd >= 0)
            );
            """
        )

    reservation = reserve_live_simulation(
        _complete(1.0),
        BudgetSpec(cost_usd_max=1.0),
        "run-upgraded",
        dry_run=False,
    )
    assert reservation is not None
    assert reservation.state == "active"
    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(simulation_reservations)")
        }
    assert {"state", "actual_usd", "accounting_unknown", "expires_at"} <= columns


def test_stale_reservations_expire_and_free_capacity(simulation_env: None) -> None:
    from omniagentos.budget.simulation import (
        expire_stale_reservations,
        reserve_live_simulation,
    )

    stale = reserve_live_simulation(
        _complete(3.0),
        BudgetSpec(cost_usd_max=3.0),
        "run-stale",
        dry_run=False,
        expires_at="2026-01-01T00:00:00Z",
    )
    assert stale is not None
    assert (
        expire_stale_reservations(
            stale.database,
            now="2026-07-30T12:00:00Z",
        )
        == 1
    )

    replacement = reserve_live_simulation(
        _complete(3.0),
        BudgetSpec(cost_usd_max=3.0),
        "run-after-expiry",
        dry_run=False,
    )
    assert replacement is not None


def test_unknown_settlement_opens_circuit_before_next_call(
    simulation_env: None,
) -> None:
    from omniagentos.budget.simulation import SimulationBudgetError, reserve_live_simulation

    reservation = reserve_live_simulation(
        _complete(1.0),
        BudgetSpec(cost_usd_max=1.0),
        "run-unknown",
        dry_run=False,
    )
    assert reservation is not None
    settled = reservation.settle(None)
    assert settled.state == "settled"
    assert settled.actual_usd is None
    assert settled.accounting_unknown is True

    with pytest.raises(SimulationBudgetError, match="circuit is open"):
        reserve_live_simulation(
            _complete(1.0),
            BudgetSpec(cost_usd_max=1.0),
            "run-after-unknown",
            dry_run=False,
        )


def test_live_executor_refuses_incomplete_ceilings_before_adapter_call(
    simulation_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omniagentos.lab.executor as executor
    from omniagentos.budget.simulation import SimulationBudgetError

    called = False

    def forbidden_adapter(_harness: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("adapter must not be reached")

    monkeypatch.setattr(executor, "resolve_adapter", forbidden_adapter)
    genome = GenomeSpec(
        id="budget-test",
        roles=[GenomeRole(name="writer", agent="mock")],
        flow=[GenomeStage(stage="draft", kind="generate", role="writer")],
        budget={"wall_min": 1, "tokens": 1000, "cost_usd": 0.5},
    )

    with pytest.raises(SimulationBudgetError, match="complete ceilings"):
        execute_genome(genome, {"prompt": "test"}, Budgets(), dry_run=False)
    assert called is False
