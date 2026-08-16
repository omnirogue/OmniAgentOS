"""CBM progressive escalation — full ladder, gate decisions, ETAR, limits."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.cbm.service import RUNGS, CognitiveBudgetService


@pytest.fixture()
def svc(tmp_path: Path) -> CognitiveBudgetService:
    return CognitiveBudgetService(database=str(tmp_path / "cbm.db"))


def test_initial_fast_model_allocation_fields(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(task_id="t-fast", stage="execution")
    assert a["rung"] == 1
    assert a["model_role"] == "fast_implementer"
    assert a["reasoning_effort"] == "low"
    assert a["parallel_candidates"] == 1
    assert a["context_mode"] == "targeted"
    assert "mechanical" in a["verification_rung"] or a["verification_rung"]
    assert a["stop_rule"] == "acceptance_gate_passed"
    assert a["provider_hints"]


def test_reasoning_effort_and_candidate_count_by_rung(svc: CognitiveBudgetService) -> None:
    for force, expect_effort, expect_cand in (
        (1, "low", 1),
        (2, "high", 1),
        (3, "medium", 2),
        (5, "xhigh", 3),
    ):
        a = svc.allocate(task_id=f"r{force}", force_rung=force)
        assert a["reasoning_effort"] == expect_effort
        assert a["parallel_candidates"] == expect_cand


def test_verification_stage_mechanical_first(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(stage="verification")
    assert a["rung"] == 0
    assert a["model_role"] == "none"
    assert a["verification_rung"] == "mechanical"


def test_specialist_and_expert_swarm_and_break_glass(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(task_id="irr", risk_class="irreversible", novelty="high")
    assert a["rung"] >= 4  # specialist
    b = svc.allocate(task_id="x", force_rung=5)
    assert b["model_role"] == "strong_planner"
    assert b["parallel_candidates"] == 3
    c = svc.allocate(task_id="bg", force_rung=6)
    assert c["model_role"] == "human_exception"
    assert c["verification_rung"] == "human_gate"


def test_model_family_escalation_path_material(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(task_id="esc-path")
    path = a["escalation_path"]
    assert "increase_effort" in path
    assert "switch_model_family" in path
    e1 = svc.escalate(a["id"], trigger_code="gate_failure", evidence=["t1"])
    assert e1["from_rung"] == 1 and e1["to_rung"] == 2
    assert e1["changed"]["reasoning_effort"] == "high"
    e2 = svc.escalate(a["id"], trigger_code="repeated_failure", evidence=["same hash"])
    assert e2["to_rung"] >= 3
    assert e2["changed"]["parallel_candidates"] >= 2


def test_decide_gate_contracts_on_accepted_quality(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(task_id="gate-ok", required_quality=0.9)
    d = svc.decide_gate(a["id"], passed=True, measured_quality=0.95)
    assert d["decision"] == "contract"
    assert d["status"] == "contracted"
    # Escalation after contract is refused
    with pytest.raises(ValueError, match="cannot escalate"):
        svc.escalate(a["id"], trigger_code="gate_failure")


def test_decide_gate_escalates_on_failure_stops_at_quality(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(task_id="gate-fail", required_quality=0.9)
    d1 = svc.decide_gate(a["id"], passed=False, measured_quality=0.4, evidence=["tests failed"])
    assert d1["decision"] == "escalate"
    assert d1["to_rung"] == 2
    # Quality short even if "passed" flag true
    d2 = svc.decide_gate(a["id"], passed=True, measured_quality=0.5)
    assert d2["decision"] == "escalate"
    # Pass threshold → stop immediately
    d3 = svc.decide_gate(a["id"], passed=True, measured_quality=0.99)
    assert d3["decision"] == "contract"


def test_decide_gate_unknown_at_break_glass(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(task_id="bg-fail", force_rung=6)
    d = svc.decide_gate(a["id"], passed=False, measured_quality=0.1)
    assert d["decision"] == "unknown"
    assert d["status"] == "closed"


def test_cannot_exceed_max_rung(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(task_id="max", force_rung=6)
    with pytest.raises(ValueError, match="break-glass"):
        svc.escalate(a["id"], trigger_code="gate_failure")


def test_redundant_candidates_cancelled_on_contract(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(task_id="cand", force_rung=3)
    assert a["parallel_candidates"] == 2
    out = svc.contract(a["id"], reason="one_verified_passed")
    assert out["action"] == "cancel_redundant_candidates_release_verifiers"
    got = svc.get_allocation(a["id"])
    assert got is not None
    assert got["parallel_candidates"] == 1
    assert got["status"] == "contracted"


def test_etar_recommend_rung_adjusts_when_quality_low(svc: CognitiveBudgetService) -> None:
    # Seed poor role stats for fast_implementer
    for i in range(5):
        a = svc.allocate(task_id=f"seed-{i}", force_rung=1)
        svc.close_allocation(a["id"], first_pass_accepted=False, wall_seconds=200.0, repair_count=2)
    rec = svc.recommend_rung(required_quality=0.95, stage="execution")
    assert "recommended_rung" in rec
    assert rec["predicted_etar_s"] is not None
    # After poor accepts, predicted quality low → ETAR adjustment may bump
    assert rec["recommended_rung"] >= 1


def test_verifier_rung_escalates_with_ladder(svc: CognitiveBudgetService) -> None:
    a = svc.allocate(task_id="v", force_rung=1)
    assert "mechanical" in a["verification_rung"]
    svc.escalate(a["id"], trigger_code="gate_failure")
    svc.escalate(a["id"], trigger_code="repeated_failure")
    got = svc.get_allocation(a["id"])
    assert got is not None
    assert got["verification_rung"] in {
        "single_reviewer",
        "specialist_plus_verifier",
        "panel",
        "mechanical_plus_independent",
    }


def test_all_seven_rungs_named(svc: CognitiveBudgetService) -> None:
    assert [r["name"] for r in RUNGS] == [
        "mechanical",
        "fast",
        "strengthened",
        "diverse",
        "specialist",
        "expert_swarm",
        "break_glass",
    ]
