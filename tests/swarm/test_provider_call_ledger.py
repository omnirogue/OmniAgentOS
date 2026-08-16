from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.contracts import BudgetSpec
from omniagentos.lab.contracts import Budgets
from omniagentos.swarm.dal import SwarmDal
from tests.support.db_template import migrated_db


def _observation(call_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "call_id": call_id,
        "request_id": f"request-{call_id}",
        "execution_id": f"execution-{call_id}",
        "stage": "worker",
        "attempt_index": 0,
        "provider": "openai",
        "transport": "responses-api",
        "requested_model": "gpt-test",
        "effective_model": "gpt-test",
        "model_lineage": "gpt-test",
        "billing_provider": "openai",
        "adapter_key": "openai",
        "request_state": "not_sent",
        "cost_quality": "unknown",
        "cost_source": "provider",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def dal(tmp_path: Path) -> SwarmDal:
    db_path = tmp_path / "swarm.db"
    db_path = migrated_db(CollabStore, db_path)
    store = SwarmDal(db_path)
    try:
        yield store
    finally:
        store.close()


def test_idempotent_call_id(dal: SwarmDal) -> None:
    initiated = _observation("call-idempotent")
    first = dal.record_provider_call(initiated)
    replay = dal.record_provider_call(initiated)
    assert replay == first

    settlement = {
        "request_state": "sent",
        "provider_outcome": "completed",
        "cost_quality": "exact",
        "cost_usd_decimal": "0.000003705",
        "cost_usd_nanos": 3705,
        "cost_source": "provider_invoice",
        "settled_at": "2026-07-30T12:00:00Z",
    }
    settled = dal.settle_provider_call("call-idempotent", settlement)
    settled_replay = dal.settle_provider_call("call-idempotent", settlement)

    assert settled_replay == settled
    assert settled["cost_usd_decimal"] == "0.000003705"
    assert settled["cost_usd_nanos"] == 3705
    assert len(dal.list_provider_calls(execution_id="execution-call-idempotent")) == 1
    assert (
        dal.aggregate_provider_call_cost(execution_id="execution-call-idempotent")[
            "known_usd_decimal"
        ]
        == "0.000003705"
    )

    with pytest.raises(ValueError, match="conflict"):
        dal.record_provider_call({**initiated, "provider": "different-provider"})


def test_reservation_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omniagentos.budget.simulation import (
        SimulationBudgetError,
        reserve_live_simulation,
    )

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
    monkeypatch.setenv("OMNIAGENTOS_SIMULATION_CAMPAIGN_ID", "gate-campaign")
    monkeypatch.setenv(
        "OMNIAGENTOS_SIMULATION_BUDGET_DB",
        str(tmp_path / "reservations.db"),
    )
    budgets = Budgets(
        wall_minutes=5,
        tokens=1_000,
        cost_usd=1.0,
        replicates=1,
    )

    first = reserve_live_simulation(
        budgets,
        BudgetSpec(cost_usd_max=1.0),
        "reservation-replay",
        dry_run=False,
    )
    replay = reserve_live_simulation(
        budgets,
        BudgetSpec(cost_usd_max=1.0),
        "reservation-replay",
        dry_run=False,
    )
    assert replay == first

    with pytest.raises(SimulationBudgetError, match="different terms"):
        reserve_live_simulation(
            budgets,
            BudgetSpec(cost_usd_max=1.5),
            "reservation-replay",
            dry_run=False,
        )


def test_unknown_propagates(dal: SwarmDal) -> None:
    run_id = str(dal.create_run(working_dir="/tmp/ws", goal="unknown cost", source="test")["id"])
    dal.record_provider_call(
        _observation(
            "call-unknown",
            run_id=run_id,
            request_state="sent",
            settled_at="2026-07-30T12:00:00Z",
        )
    )

    aggregate = dal.aggregate_provider_call_cost(run_id=run_id)
    assert aggregate["known_usd"] is None
    assert aggregate["known_usd_decimal"] is None
    assert aggregate["chargeable_usd"] is None
    assert aggregate["quality"] == "unknown"
    assert aggregate["unknown_call_count"] == 1


def test_accounting_incomplete_surfaced(dal: SwarmDal) -> None:
    run_id = str(dal.create_run(working_dir="/tmp/ws", goal="incomplete", source="test")["id"])
    dal.record_provider_call(
        _observation(
            "call-incomplete",
            run_id=run_id,
            request_state="indeterminate",
            settled_at="2026-07-30T12:00:00Z",
        )
    )

    aggregate = dal.aggregate_provider_call_cost(run_id=run_id)
    spend = dal.budget_spend(run_id)
    assert aggregate["accounting_incomplete"] is True
    assert aggregate["safe_to_compare"] is False
    assert spend.accounting_incomplete is True
    assert spend.safe_to_compare is False
    assert spend.unknown_call_count >= 1
    assert spend.quality != "exact"
    assert spend.cost_usd is None
