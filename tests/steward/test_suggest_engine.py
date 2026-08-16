from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.steward.config import StewardConfig
from omniagentos.steward.store import StewardStore
from omniagentos.steward.suggest import (
    build_plan,
    effective_action_class,
    generate_goal_suggestions,
    reconcile_suggestion,
    task_risk_for,
)


@pytest.fixture
def steward(tmp_path: Path) -> StewardStore:
    return StewardStore(SqliteStore(str(tmp_path / "suggest_engine.db")))


def _snapshot(
    steward: StewardStore, goal_id: str, source: str, metric: str, value: float, *, days_ago: int
) -> None:
    steward.insert_metric_snapshot(
        {
            "goal_id": goal_id,
            "source": source,
            "metric": metric,
            "value": value,
            "captured_at": (datetime.now(UTC) - timedelta(days=days_ago)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
    )


def _seed_revenue_goal(steward: StewardStore) -> dict[str, object]:
    return steward.upsert_goal(
        {
            "name": "increase-revenue",
            "discipline_id": "revenue",
            "north_star": {"source": "stripe", "metric": "net_revenue_usd"},
            "status": "active",
        }
    )


def _seed_roas_goal(steward: StewardStore) -> dict[str, object]:
    return steward.upsert_goal(
        {
            "name": "improve-ad-roi",
            "discipline_id": "ad-roi",
            "north_star": {"source": "meta", "metric": "roas"},
            "status": "active",
        }
    )


def test_revenue_downtrend_triggers_on_significant_drop(steward: StewardStore) -> None:
    goal = _seed_revenue_goal(steward)
    goal_id = str(goal["id"])
    values = [100.0, 100.0, 100.0, 100.0, 70.0, 70.0, 70.0]  # 30% drop, oldest first
    for offset, value in enumerate(values):
        _snapshot(
            steward, goal_id, "stripe", "net_revenue_usd", value, days_ago=len(values) - offset
        )

    created = generate_goal_suggestions(steward, StewardConfig())

    matches = [row for row in created if row["title"] == "Investigate revenue dip"]
    assert len(matches) == 1
    row = matches[0]
    assert row["goal_id"] == goal_id
    assert row["risk_class"] == "read_only"
    assert row["state"] == "open"
    assert row["source"] == "steward.suggest.revenue_downtrend"
    assert row["proposed_plan"]["action_class"] == "read_only"
    assert row["proposed_plan"]["harness"] == "cli-claude"
    assert "revenue" in row["proposed_plan"]["prompt"].lower()
    assert row["evidence"][0]["drop_pct"] > 0.20


def test_revenue_downtrend_does_not_trigger_on_stable_revenue(steward: StewardStore) -> None:
    goal = _seed_revenue_goal(steward)
    goal_id = str(goal["id"])
    values = [100.0, 101.0, 99.0, 100.0, 100.0, 99.0, 101.0]
    for offset, value in enumerate(values):
        _snapshot(
            steward, goal_id, "stripe", "net_revenue_usd", value, days_ago=len(values) - offset
        )

    created = generate_goal_suggestions(steward, StewardConfig())

    assert created == []


def test_revenue_downtrend_does_not_trigger_with_insufficient_history(
    steward: StewardStore,
) -> None:
    goal = _seed_revenue_goal(steward)
    goal_id = str(goal["id"])
    for offset, value in enumerate([100.0, 70.0, 70.0]):
        _snapshot(steward, goal_id, "stripe", "net_revenue_usd", value, days_ago=3 - offset)

    created = generate_goal_suggestions(steward, StewardConfig())

    assert created == []


def test_roas_opportunity_triggers_when_roas_high_and_spend_flat(steward: StewardStore) -> None:
    goal = _seed_roas_goal(steward)
    goal_id = str(goal["id"])
    for offset, value in enumerate([500.0, 505.0, 495.0, 500.0]):
        _snapshot(steward, goal_id, "meta", "spend_usd", value, days_ago=4 - offset)
    _snapshot(steward, goal_id, "meta", "roas", 2.5, days_ago=1)

    created = generate_goal_suggestions(steward, StewardConfig())

    matches = [row for row in created if row["title"] == "Consider scaling winning campaigns"]
    assert len(matches) == 1
    row = matches[0]
    assert row["goal_id"] == goal_id
    assert row["risk_class"] == "read_only"
    assert row["source"] == "steward.suggest.roas_opportunity"
    assert row["proposed_plan"]["action_class"] == "read_only"
    assert "change nothing" in row["proposed_plan"]["prompt"].lower() or "analysis only" in (
        row["proposed_plan"]["prompt"].lower()
    )


def test_roas_opportunity_does_not_trigger_when_spend_volatile(steward: StewardStore) -> None:
    goal = _seed_roas_goal(steward)
    goal_id = str(goal["id"])
    for offset, value in enumerate([300.0, 900.0, 200.0, 800.0]):
        _snapshot(steward, goal_id, "meta", "spend_usd", value, days_ago=4 - offset)
    _snapshot(steward, goal_id, "meta", "roas", 2.5, days_ago=1)

    created = generate_goal_suggestions(steward, StewardConfig())

    assert created == []


def test_roas_opportunity_does_not_trigger_below_roas_threshold(steward: StewardStore) -> None:
    goal = _seed_roas_goal(steward)
    goal_id = str(goal["id"])
    for offset, value in enumerate([500.0, 505.0, 495.0, 500.0]):
        _snapshot(steward, goal_id, "meta", "spend_usd", value, days_ago=4 - offset)
    _snapshot(steward, goal_id, "meta", "roas", 1.5, days_ago=1)

    created = generate_goal_suggestions(steward, StewardConfig())

    assert created == []


def test_generate_goal_suggestions_dedupes_open_suggestions(steward: StewardStore) -> None:
    goal = _seed_revenue_goal(steward)
    goal_id = str(goal["id"])
    values = [100.0, 100.0, 100.0, 100.0, 70.0, 70.0, 70.0]
    for offset, value in enumerate(values):
        _snapshot(
            steward, goal_id, "stripe", "net_revenue_usd", value, days_ago=len(values) - offset
        )

    first = generate_goal_suggestions(steward, StewardConfig())
    second = generate_goal_suggestions(steward, StewardConfig())

    assert len(first) == 1
    assert second == []
    assert len(steward.list_suggestions(state="open")) == 1


def test_generate_goal_suggestions_allows_new_suggestion_once_prior_is_decided(
    steward: StewardStore,
) -> None:
    goal = _seed_revenue_goal(steward)
    goal_id = str(goal["id"])
    values = [100.0, 100.0, 100.0, 100.0, 70.0, 70.0, 70.0]
    for offset, value in enumerate(values):
        _snapshot(
            steward, goal_id, "stripe", "net_revenue_usd", value, days_ago=len(values) - offset
        )

    first = generate_goal_suggestions(steward, StewardConfig())
    assert len(first) == 1
    steward.decide_suggestion(str(first[0]["id"]), state="dismissed", decided_by="owner")

    second = generate_goal_suggestions(steward, StewardConfig())
    assert len(second) == 1


def test_generate_goal_suggestions_returns_nothing_below_rung_1(steward: StewardStore) -> None:
    goal = _seed_revenue_goal(steward)
    goal_id = str(goal["id"])
    values = [100.0, 100.0, 100.0, 100.0, 70.0, 70.0, 70.0]
    for offset, value in enumerate(values):
        _snapshot(
            steward, goal_id, "stripe", "net_revenue_usd", value, days_ago=len(values) - offset
        )

    cfg = StewardConfig()
    cfg.autonomy.rung = 0

    created = generate_goal_suggestions(steward, cfg)

    assert created == []


def test_build_plan_carries_action_class_and_prompt() -> None:
    suggestion = {
        "title": "x",
        "proposed_plan": {
            "prompt": "do the thing",
            "harness": "cli-claude",
            "action_class": "consequential",
        },
    }

    plan, prompt = build_plan(suggestion)

    assert prompt == "do the thing"
    assert plan == [
        {
            "name": "agent",
            "kind": "agent",
            "action_class": "consequential",
            "params": {"prompt": "do the thing"},
        }
    ]


def test_build_plan_defaults_action_class_when_absent() -> None:
    suggestion = {"proposed_plan": {"prompt": "look around"}}

    plan, prompt = build_plan(suggestion)

    assert prompt == "look around"
    assert plan[0]["action_class"] == "sandboxed_creation"


def test_build_plan_handles_missing_proposed_plan() -> None:
    plan, prompt = build_plan({"title": "no plan"})

    assert prompt == ""
    assert plan[0]["params"]["prompt"] == ""
    assert plan[0]["action_class"] == "sandboxed_creation"


# --- H13: risk_class <-> action_class fidelity -----------------------------


def test_build_plan_derives_action_class_from_risk_class_when_plan_omits_it() -> None:
    # RD's alert-remediation suggestions arrive with a risk_class but no
    # proposed_plan.action_class. Without the fix build_plan silently ran them
    # as 'sandboxed_creation' while the card claimed the risk_class -- a
    # mismatch between what is shown and what executes. Now the plan step must
    # MATCH the declared risk_class.
    suggestion = {
        "title": "remediate alert",
        "risk_class": "internal_reversible",
        "proposed_plan": {"prompt": "roll back the config change"},
    }

    plan, _ = build_plan(suggestion)

    assert plan[0]["action_class"] == "internal_reversible"


def test_build_plan_prefers_explicit_plan_action_class_over_risk_class() -> None:
    # The plan's own action_class is the ground truth of what runs; if it is
    # higher than the (stale/under-stated) risk_class, execution must not be
    # lowered to the risk_class.
    suggestion = {
        "title": "x",
        "risk_class": "read_only",
        "proposed_plan": {"prompt": "do it", "action_class": "consequential"},
    }

    plan, _ = build_plan(suggestion)

    assert plan[0]["action_class"] == "consequential"


def test_effective_action_class_precedence() -> None:
    assert (
        effective_action_class(
            {"risk_class": "read_only", "proposed_plan": {"action_class": "consequential"}}
        )
        == "consequential"
    )
    assert effective_action_class({"risk_class": "external_reversible"}) == "external_reversible"
    assert effective_action_class({"proposed_plan": {"prompt": "x"}}) == "sandboxed_creation"
    # An unknown/garbage class is ignored, not trusted.
    assert effective_action_class({"risk_class": "totally-made-up"}) == "sandboxed_creation"


def test_effective_action_class_preserves_irreversible() -> None:
    # Regression: _ACTION_CLASSES previously omitted "irreversible", so a
    # stored irreversible suggestion silently rewrote to the sandboxed
    # default here (and via reconcile_suggestion, on every list/approve
    # response) -- reopening the dashboard's RISK_CLASSES fail-open defect
    # end-to-end even after the frontend fix landed.
    assert effective_action_class({"risk_class": "irreversible"}) == "irreversible"
    assert (
        effective_action_class({"proposed_plan": {"action_class": "irreversible"}})
        == "irreversible"
    )


@pytest.mark.parametrize(
    ("action_class", "expected_risk"),
    [
        ("read_only", "low"),
        ("sandboxed_creation", "low"),
        ("internal_reversible", "medium"),
        ("external_reversible", "medium"),
        ("consequential", "high"),
        ("irreversible", "high"),
    ],
)
def test_task_risk_for_maps_every_action_class(action_class: str, expected_risk: str) -> None:
    suggestion = {"proposed_plan": {"action_class": action_class}}
    assert task_risk_for(suggestion) == expected_risk


def test_reconcile_suggestion_preserves_irreversible_risk_class() -> None:
    # Red-first for the server-side coercion defect: a stored
    # risk_class="irreversible" suggestion must survive reconcile_suggestion
    # (called on every list/approve response) as "irreversible", not get
    # silently rewritten to the sandboxed default.
    suggestion = {
        "id": "sug_irreversible",
        "risk_class": "irreversible",
        "evidence": [],
        "proposed_plan": {"prompt": "p", "harness": "cli-claude", "action_class": "irreversible"},
    }

    reconciled = reconcile_suggestion(suggestion)

    assert reconciled["risk_class"] == "irreversible"
    assert reconciled["proposed_plan"]["action_class"] == "irreversible"
    assert task_risk_for(suggestion) == "high"


def test_reconcile_suggestion_makes_card_match_execution() -> None:
    # A card that under-states its risk (risk_class read_only, plan consequential)
    # must be reconciled UP to what actually runs -- the operator sees the truth.
    suggestion = {
        "id": "sug_1",
        "risk_class": "read_only",
        "evidence": [{"metric": "x"}],
        "proposed_plan": {"prompt": "p", "harness": "cli-claude", "action_class": "consequential"},
    }

    reconciled = reconcile_suggestion(suggestion)

    assert reconciled["risk_class"] == "consequential"
    assert reconciled["proposed_plan"]["action_class"] == "consequential"
    # Frozen shape for RF: prompt/harness/evidence preserved.
    assert reconciled["proposed_plan"]["prompt"] == "p"
    assert reconciled["proposed_plan"]["harness"] == "cli-claude"
    assert reconciled["evidence"] == [{"metric": "x"}]
    # The input is not mutated.
    assert suggestion["risk_class"] == "read_only"
