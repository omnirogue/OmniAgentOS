from __future__ import annotations

from dataclasses import dataclass

from omniagentos.allocation.arbiter import decide_route
from omniagentos.allocation.characterize import characterize
from omniagentos.allocation.fanout import FanoutDecision


@dataclass(frozen=True)
class MockFormationBinding:
    topology: str | None
    low_confidence: bool


def test_decide_route_rule_1_solo_true():
    # solo is True -> solo_strong, sequential, 1 worker.
    # Supply enough named signals that confidence clears the threshold, so this
    # test isolates rule 1 (solo) rather than accidentally passing via rule 2
    # (low confidence) -- both routes return solo_strong, so a low-confidence
    # fixture here would prove nothing.
    char = characterize(
        {
            "multi_specialty": 1.0,
            "risk": 0.5,
            "verifiable": 0.5,
            "uncertainty": 0.2,
            "sequential": 0.0,
            "decomposable": 0.5,
            "independence": 0.5,
            "critical": 0.2,
            "knowledge_heavy": 0.1,
            "exploratory": 0.1,
            "work_volume": 2.0,
            "urgency": 0.3,
            "independent_units": 3,
        }
    )
    assert char.confidence >= 0.5
    fanout = FanoutDecision("specialist_panel", 3, 1, 4, "ok")

    decision = decide_route(char, fanout, solo=True)
    assert decision.route == "solo_strong"
    assert decision.topology == "sequential"
    assert decision.worker_count == 1
    assert "solo is True" in decision.rationale


def test_decide_route_rule_2_low_confidence():
    # char.confidence < min_confidence -> solo_strong, sequential, 1
    # We force confidence to be very low by passing zero signals
    char = characterize({})
    assert char.confidence < 0.5
    fanout = FanoutDecision("specialist_panel", 3, 1, 4, "ok")

    decision = decide_route(char, fanout, min_confidence=0.5)
    assert decision.route == "solo_strong"
    assert decision.topology == "sequential"
    assert decision.worker_count == 1
    assert "min_confidence" in decision.rationale


def test_decide_route_rule_3_sequential_shaped():
    # char.S >= 0.6 -> solo_strong, sequential, 1
    char = characterize(
        {
            "S": 0.8,
            "sequential": 1.0,
            "risk": 0.5,
            "verifiable": 0.5,
            "multi_specialty": 1.0,
            "urgency": 1.0,
            "work_volume": 1.0,
        }
    )
    assert char.confidence >= 0.5
    fanout = FanoutDecision("sequential", 1, 1, 4, "ok")

    decision = decide_route(char, fanout)
    assert decision.route == "solo_strong"
    assert decision.topology == "sequential"
    assert decision.worker_count == 1
    assert "char.S" in decision.rationale


def test_decide_route_rule_4_hard_capacity_low():
    # fanout_decision.hard_capacity <= 1 -> solo_strong, sequential, 1
    char = characterize(
        {
            "M": 0.9,
            "multi_specialty": 1.0,
            "risk": 0.5,
            "verifiable": 0.5,
            "urgency": 1.0,
            "work_volume": 1.0,
        }
    )
    assert char.confidence >= 0.5
    fanout = FanoutDecision("specialist_panel", 3, 1, 1, "ok")

    decision = decide_route(char, fanout)
    assert decision.route == "solo_strong"
    assert decision.topology == "sequential"
    assert decision.worker_count == 1
    assert "hard_capacity" in decision.rationale


def test_decide_route_rule_5_multi_specialty():
    # char.M >= 0.7 -> parallel_review, specialist_panel
    char = characterize(
        {
            "M": 0.8,
            "multi_specialty": 1.0,
            "risk": 0.5,
            "verifiable": 0.5,
            "urgency": 1.0,
            "work_volume": 1.0,
        }
    )
    assert char.confidence >= 0.5
    fanout = FanoutDecision("specialist_panel", 3, 1, 4, "ok")

    decision = decide_route(char, fanout)
    assert decision.route == "parallel_review"
    assert decision.topology == "specialist_panel"
    # Worker count clamp logic for specialist_panel (cap is 3):
    # min(3, 4, 3) = 3 -> clamp to [2, 4] -> 3 -> min(3, 4, 3) = 3
    assert decision.worker_count == 3
    assert "char.M" in decision.rationale


def test_decide_route_rule_6_uncertain_verifiable():
    # char.U >= 0.7 and char.V >= 0.5 -> parallel_review, generator_critic
    char = characterize(
        {
            "U": 0.8,
            "uncertainty": 1.0,
            "V": 0.6,
            "verifiable": 1.0,
            "risk": 0.5,
            "urgency": 1.0,
            "work_volume": 1.0,
        }
    )
    assert char.confidence >= 0.5
    fanout = FanoutDecision("generator_critic", 2, 1, 4, "ok")

    decision = decide_route(char, fanout)
    assert decision.route == "parallel_review"
    assert decision.topology == "generator_critic"
    # cap for generator_critic is 2.
    assert decision.worker_count == 2
    assert "char.U" in decision.rationale


def test_decide_route_rule_7_decomposable_independent():
    # char.D >= 0.6 and char.I >= 0.5 -> centralized_team, map_reduce
    char = characterize(
        {
            "D": 0.7,
            "I": 0.6,
            "has_partitions": 1.0,
            "risk": 0.5,
            "verifiable": 0.5,
            "urgency": 1.0,
            "work_volume": 1.0,
        }
    )
    assert char.confidence >= 0.5
    fanout = FanoutDecision("map_reduce", 4, 1, 4, "ok")

    decision = decide_route(char, fanout)
    assert decision.route == "centralized_team"
    assert decision.topology == "map_reduce"
    # cap for map_reduce is hard_capacity = 4.
    # min(4, 4, 4) = 4 -> clamp to [2, 4] -> 4 -> min(4, 4, 4) = 4
    assert decision.worker_count == 4
    assert "char.D" in decision.rationale


def test_decide_route_rule_8_otherwise():
    # otherwise -> centralized_team, hierarchical
    # We ensure none of M >= 0.7, (U >= 0.7 and V >= 0.5), (D >= 0.6 and I >= 0.5) match,
    # and S < 0.6.
    char = characterize(
        {
            "M": 0.3,
            "U": 0.3,
            "V": 0.3,
            "D": 0.3,
            "I": 0.3,
            "S": 0.1,
            "risk": 0.5,
            "verifiable": 0.5,
            "urgency": 1.0,
            "work_volume": 1.0,
        }
    )
    assert char.confidence >= 0.5
    fanout = FanoutDecision("hierarchical", 4, 1, 4, "ok")

    decision = decide_route(char, fanout)
    assert decision.route == "centralized_team"
    assert decision.topology == "hierarchical"
    assert decision.worker_count == 4
    assert "otherwise" in decision.rationale


def test_property_sequential_shaped_never_parallel():
    # property: a sequential-shaped task (high S) never yields worker_count > 1
    # We test with different hard capacities, and worker counts
    char = characterize(
        {
            "S": 0.9,
            "sequential": 1.0,
            "risk": 0.5,
            "verifiable": 0.5,
            "urgency": 1.0,
            "work_volume": 1.0,
        }
    )

    for h_cap in range(1, 10):
        for w_count in range(1, 10):
            fanout = FanoutDecision("sequential", w_count, 1, h_cap, "ok")
            decision = decide_route(char, fanout)
            assert decision.worker_count == 1
            assert decision.topology == "sequential"
            assert decision.route == "solo_strong"


def test_worker_count_caps_and_clamps():
    # Test that worker count respects topology caps:
    # sequential: cap 1
    # generator_critic: cap 2
    # specialist_panel: cap 3
    # map_reduce: hard_capacity
    # hierarchical: hard_capacity

    char = characterize(
        {
            "M": 0.8,
            "multi_specialty": 1.0,
            "risk": 0.5,
            "verifiable": 0.5,
            "urgency": 1.0,
            "work_volume": 1.0,
        }
    )  # default specialist_panel

    # Cap first, clamp to [2, 4], then cap again.
    # If topology is sequential, cap is 1.
    # If overridden by formation topology = sequential, worker_count must be 1.
    formation_seq = MockFormationBinding(topology="sequential", low_confidence=False)
    fanout = FanoutDecision("specialist_panel", 4, 1, 4, "ok")
    decision = decide_route(char, fanout, formation_binding=formation_seq)
    assert decision.topology == "sequential"
    assert decision.worker_count == 1
    assert decision.route == "solo_strong"

    # If topology is specialist_panel, cap is 3. Even with hard_capacity = 4 and worker_count = 4,
    # it must cap down to 3.
    decision_panel = decide_route(char, fanout)
    assert decision_panel.topology == "specialist_panel"
    assert decision_panel.worker_count == 3

    # If topology is generator_critic, cap is 2. Even with hard_capacity = 4 and worker_count = 4,
    # it must cap down to 2.
    formation_gc = MockFormationBinding(topology="generator_critic", low_confidence=False)
    decision_gc = decide_route(char, fanout, formation_binding=formation_gc)
    assert decision_gc.topology == "generator_critic"
    assert decision_gc.worker_count == 2


def test_formation_binding_override_precedence():
    # Confident formation overrides rule topology.
    char = characterize(
        {
            "M": 0.3,
            "U": 0.3,
            "V": 0.3,
            "D": 0.3,
            "I": 0.3,
            "S": 0.1,
            "risk": 0.5,
            "verifiable": 0.5,
            "urgency": 1.0,
            "work_volume": 1.0,
        }
    )  # otherwise would be hierarchical
    fanout = FanoutDecision("hierarchical", 4, 1, 4, "ok")

    # Confident formation
    formation = MockFormationBinding(topology="generator_critic", low_confidence=False)
    decision = decide_route(char, fanout, formation_binding=formation)
    assert decision.topology == "generator_critic"
    assert decision.route == "parallel_review"
    assert "overridden by confident formation topology" in decision.rationale

    # Low-confidence formation: does NOT override.
    formation_low = MockFormationBinding(topology="generator_critic", low_confidence=True)
    decision_low = decide_route(char, fanout, formation_binding=formation_low)
    assert decision_low.topology == "hierarchical"
    assert decision_low.route == "centralized_team"
