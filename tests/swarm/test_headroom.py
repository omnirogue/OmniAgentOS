"""Rate-limit-aware solo-vs-swarm rule: ``swarm_headroom`` + its planner wiring.

LOW fleet headroom (few free swarm session slots, or most enabled providers
cooling/saturated) RAISES the solo threshold to ``auto.low_headroom_ratio`` so
parallelism must pay harder before a swarm spends scarce rate-limit budget;
HIGH headroom keeps the standard 1.5 rule. The decision and its inputs are
recorded in ``plan_json.assumptions``. Classification is pure given fake limit
inputs; live reads are scoped to an explicit ``db_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.swarm.planner import (
    DEFAULT_LOW_HEADROOM_RATIO,
    DEFAULT_LOW_HEADROOM_SLOTS,
    SOLO_RATIO_THRESHOLD,
    SwarmHeadroom,
    build_plan,
    fast_speed_headroom,
    plan_payload,
    plan_swarm_bundles,
    swarm_headroom,
)


def _snapshot(level: str, threshold: float) -> SwarmHeadroom:
    return SwarmHeadroom(
        level=level,  # type: ignore[arg-type]
        available_for_swarm=20 if level == "high" else 1,
        mean_provider_pressure=0.1 if level == "high" else 0.9,
        low_headroom_slots=DEFAULT_LOW_HEADROOM_SLOTS,
        solo_ratio_threshold=threshold,
    )


class TestFastSpeedHeadroom:
    """Fastest-dial topology bias: speed='fast' raises the solo threshold."""

    def test_raises_a_high_headroom_threshold_to_the_low_bar(self) -> None:
        raised = fast_speed_headroom(_snapshot("high", SOLO_RATIO_THRESHOLD))
        assert raised is not None
        assert raised.solo_ratio_threshold == DEFAULT_LOW_HEADROOM_RATIO
        # Every other field rides through untouched.
        assert raised.level == "high"
        assert raised.available_for_swarm == 20

    def test_never_lowers_an_already_raised_threshold(self) -> None:
        low = _snapshot("low", 3.0)
        assert fast_speed_headroom(low) is low

    def test_none_headroom_fault_stays_none(self) -> None:
        assert fast_speed_headroom(None) is None


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    db = str(tmp_path / "headroom.db")
    CollabStore(db)  # migrates the shared schema (sessions, claude_accounts, ...)
    return db


def _raw_task(task_id: str, deps: tuple[str, ...] = (), agent: int = 10) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": task_id.upper(),
        "description": f"do {task_id}",
        "depends_on": list(deps),
        "owned_paths": [f"pkg/{task_id}"],
        "est_agent_minutes": agent,
        "est_manual_minutes": 30,
        "acceptance": f"{task_id} done",
        "verify_command": f"pytest tests/{task_id}",
    }


def _ratio_two_tasks() -> list[dict[str, Any]]:
    """Four worker tasks, total 40 agent-min, critical path 20 → ratio 2.0.

    2.0 sits exactly BETWEEN the standard 1.5 rule and the raised 2.5 LOW-
    headroom threshold — the discriminating case for the headroom decision."""
    return [
        _raw_task("a"),
        _raw_task("b", deps=("a",)),
        _raw_task("c"),
        _raw_task("d", deps=("c",)),
    ]


def _headroom(level: str) -> SwarmHeadroom:
    if level == "low":
        return swarm_headroom(available_for_swarm=2, provider_pressures=[0.9], config={})
    return swarm_headroom(available_for_swarm=40, provider_pressures=[0.1], config={})


class TestSwarmHeadroomClassification:
    def test_low_when_few_swarm_slots_free(self) -> None:
        headroom = swarm_headroom(available_for_swarm=2, provider_pressures=[0.0], config={})
        assert headroom.level == "low"
        assert headroom.available_for_swarm == 2
        assert headroom.low_headroom_slots == DEFAULT_LOW_HEADROOM_SLOTS
        assert headroom.solo_ratio_threshold == DEFAULT_LOW_HEADROOM_RATIO

    def test_low_when_most_providers_cooling(self) -> None:
        headroom = swarm_headroom(available_for_swarm=40, provider_pressures=[0.9, 0.8], config={})
        assert headroom.level == "low"
        assert headroom.mean_provider_pressure == 0.85
        assert headroom.solo_ratio_threshold == DEFAULT_LOW_HEADROOM_RATIO

    def test_high_keeps_the_standard_rule(self) -> None:
        headroom = swarm_headroom(available_for_swarm=40, provider_pressures=[0.2, 0.1], config={})
        assert headroom.level == "high"
        assert headroom.solo_ratio_threshold == SOLO_RATIO_THRESHOLD

    def test_no_enabled_provider_counts_as_fully_pressured(self) -> None:
        headroom = swarm_headroom(available_for_swarm=40, provider_pressures=[], config={})
        assert headroom.level == "low"
        assert headroom.mean_provider_pressure == 1.0

    def test_config_knobs_override_defaults(self) -> None:
        config = {"auto": {"low_headroom_slots": 50, "low_headroom_ratio": 3.5}}
        headroom = swarm_headroom(available_for_swarm=40, provider_pressures=[0.1], config=config)
        assert headroom.level == "low"  # 40 < the raised slot floor of 50
        assert headroom.low_headroom_slots == 50
        assert headroom.solo_ratio_threshold == 3.5

    def test_garbage_config_falls_back_to_defaults(self) -> None:
        config = {"auto": {"low_headroom_slots": "many", "low_headroom_ratio": None}}
        headroom = swarm_headroom(available_for_swarm=2, provider_pressures=[0.0], config=config)
        assert headroom.low_headroom_slots == DEFAULT_LOW_HEADROOM_SLOTS
        assert headroom.solo_ratio_threshold == DEFAULT_LOW_HEADROOM_RATIO

    def test_live_reads_are_scoped_to_db_path(self, db_path: str) -> None:
        """A fresh control plane (no accounts, no sessions) classifies LOW —
        nothing enabled means no capacity to swarm with. Exercises the real
        fleet_available/enabled_providers wiring against an explicit db."""
        headroom = swarm_headroom(config={}, db_path=db_path)
        assert headroom.level == "low"
        assert headroom.mean_provider_pressure == 1.0
        # The empty fleet has ample raw slots — the pressure term is what binds.
        assert headroom.available_for_swarm >= headroom.low_headroom_slots

    def test_live_slot_read_with_faked_pressures_classifies_high(self, db_path: str) -> None:
        headroom = swarm_headroom(provider_pressures=[0.0], config={}, db_path=db_path)
        assert headroom.level == "high"
        assert headroom.solo_ratio_threshold == SOLO_RATIO_THRESHOLD


class TestHeadroomSoloRule:
    def test_low_headroom_forces_solo_at_ratio_two(self) -> None:
        plan = build_plan("goal", _ratio_two_tasks(), headroom=_headroom("low"))
        assert plan.parallelism_ratio == 2.0
        assert plan.mode == "solo"
        assert plan.target_n == 1
        assert plan.integration_task_id is None

    def test_high_headroom_swarms_at_ratio_two(self) -> None:
        plan = build_plan("goal", _ratio_two_tasks(), headroom=_headroom("high"))
        assert plan.parallelism_ratio == 2.0
        assert plan.mode == "swarm"

    def test_no_headroom_keeps_the_standard_rule_with_no_note(self) -> None:
        plan = build_plan("goal", _ratio_two_tasks())
        assert plan.mode == "swarm"  # 2.0 >= 1.5, exactly as before
        assert not any("auto headroom" in note for note in plan.assumptions)

    def test_decision_and_inputs_are_recorded_in_plan_json(self) -> None:
        plan = build_plan("goal", _ratio_two_tasks(), headroom=_headroom("low"))
        notes = [note for note in plan.assumptions if "auto headroom" in note]
        assert len(notes) == 1
        note = notes[0]
        assert "LOW" in note
        assert "available_for_swarm=2" in note
        assert "mean_provider_pressure=0.9" in note
        assert "ratio=2.0 -> solo" in note
        # The same note rides plan_json (swarm_runs.plan_json → UI/summary).
        assert note in plan_payload(plan)["assumptions"]

        swarm_note = next(
            note
            for note in build_plan(
                "goal", _ratio_two_tasks(), headroom=_headroom("high")
            ).assumptions
            if "auto headroom" in note
        )
        assert "HIGH" in swarm_note
        assert "ratio=2.0 -> swarm" in swarm_note

    def test_headroom_threads_through_plan_swarm_bundles(self, tmp_path: Path) -> None:
        response = {"goal": "goal", "tasks": _ratio_two_tasks()}
        common: dict[str, Any] = {
            "planner_llm": lambda prompt, schema, effort: dict(response),
            "clarify_llm": lambda prompt, schema: {
                "mode": "spec",
                "spec": {"title": "goal", "description": "goal"},
            },
            "recall_fn": lambda goal: "",
            "playbook_path": tmp_path / "missing.json",
        }
        low = plan_swarm_bundles("goal", str(tmp_path), headroom=_headroom("low"), **common)
        high = plan_swarm_bundles("goal", str(tmp_path), headroom=_headroom("high"), **common)
        assert [plan.mode for plan in low] == ["solo"]
        assert [plan.mode for plan in high] == ["swarm"]
        assert any("auto headroom LOW" in note for note in low[0].assumptions)
        assert any("auto headroom HIGH" in note for note in high[0].assumptions)
