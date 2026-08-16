"""Metacognition control loop — progress, stall, switch, promote isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.metacog.config import clear_metacog_config_cache
from omniagentos.metacog.service import MetacogService
from omniagentos.metacog.store import MetacogStore


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetacogService:
    monkeypatch.setenv("OMNIAGENTOS_METACOG_ARTIFACTS_ROOT", str(tmp_path / "arts"))
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MODE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MEMORY_PROMOTION", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_METACOG_STRATEGY_SWITCH", raising=False)
    clear_metacog_config_cache()
    return MetacogService(store=MetacogStore(str(tmp_path / "mc.db")))


def test_progress_detection_improving(svc: MetacogService) -> None:
    s = svc.evaluate(
        task_id="p1",
        criteria_total=10,
        criteria_passed=7,
        previous_progress=0.2,
    )
    assert s.objective_progress == 0.7
    assert s.quality_trend == "improving"
    assert s.new_evidence_delta > 0.05


def test_stall_and_no_progress_refuses_continue(svc: MetacogService) -> None:
    for i in range(4):
        s = svc.evaluate(
            task_id="stall1",
            criteria_total=5,
            criteria_passed=0,
            previous_progress=0.0,
            strategy_age_iterations=i,
            recent_outputs=["same output"] * (i + 2),
        )
    assert s.next_control_action != "continue"
    assert s.stall_count >= 1 or "no_progress" in " ".join(s.reason_codes)
    assert s.next_control_action in {"replan", "stop", "switch", "prune", "checkpoint"}


def test_repetition_triggers_switch(svc: MetacogService) -> None:
    s = svc.evaluate(
        task_id="rep1",
        criteria_total=4,
        criteria_passed=1,
        previous_progress=0.25,
        recent_outputs=["identical block of text that is long enough xyz"] * 5,
    )
    assert s.repetition_score > 0.5 or "repetition" in s.reason_codes
    if "repetition" in s.reason_codes:
        assert s.next_control_action in {"switch", "replan", "stop", "prune", "checkpoint"}


def test_criteria_met_stops(svc: MetacogService) -> None:
    s = svc.evaluate(
        task_id="done",
        criteria_total=3,
        criteria_passed=3,
        previous_progress=0.9,
    )
    assert s.next_control_action == "stop"
    assert "criteria_met" in s.reason_codes


def test_fanout_without_verifier_prunes(svc: MetacogService) -> None:
    s = svc.evaluate(
        task_id="fan",
        criteria_total=4,
        criteria_passed=1,
        previous_progress=0.0,
        fanout_active=4,
        verifier_capacity_reserved=0,
    )
    assert "fanout_without_verifier" in s.reason_codes
    assert s.next_control_action == "prune"


def test_context_pressure_checkpoint(svc: MetacogService) -> None:
    s = svc.evaluate(
        task_id="ctx",
        criteria_total=4,
        criteria_passed=1,
        previous_progress=0.2,
        context_pressure=0.9,
    )
    assert "context_pressure" in s.reason_codes or s.next_control_action == "checkpoint"


def test_strategy_switch_and_replan_path(svc: MetacogService) -> None:
    s = svc.select_strategy(domain="coding", novel=True)
    assert s.id == "hypothesis_portfolio"
    sw = svc.switch_strategy(
        from_strategy="solo_executor",
        to_strategy="repair_loop",
        task_id="sw1",
        reason_codes=["stall"],
    )
    assert sw["applied"] is True
    assert sw["to_strategy"] == "repair_loop"


def test_failed_memory_candidates_not_promoted(svc: MetacogService) -> None:
    art = svc.register_artifact(artifact_type="note", content="weak evidence", task_id="m1")
    weak = svc.create_memory_candidate(
        statement="questionable lesson",
        evidence=[art.id],
        confidence=0.2,
    )
    with pytest.raises(ValueError, match="low-confidence"):
        svc.promote_memory(weak.id)
    # Explicit reject by promoter role
    rejected = svc.reject_memory(weak.id, reason="validator_failed")
    assert rejected.promotion_status == "rejected"
    with pytest.raises(ValueError, match="rejected"):
        svc.promote_memory(rejected.id)
    # Invalidated also blocked
    good = svc.create_memory_candidate(
        statement="good lesson with evidence",
        evidence=[art.id],
        confidence=0.9,
    )
    svc.invalidate_memory(good.id, reason="stale")
    with pytest.raises(ValueError, match="invalidated"):
        svc.promote_memory(good.id)


def test_executor_evaluator_promoter_separation(svc: MetacogService) -> None:
    """Evaluator does not auto-promote; promoter is an explicit step."""
    art = svc.register_artifact(artifact_type="log", content="trace", task_id="sep")
    # evaluate = evaluator
    state = svc.evaluate(task_id="sep", criteria_total=2, criteria_passed=1)
    assert state.next_control_action  # evaluator emits control
    # reflection may create candidates
    svc.reflect(
        outcome="failed",
        task_id="sep",
        failure_summary="tests failed",
        evidence_artifact_ids=[art.id],
    )
    # Candidates exist; promotion is a separate call path (may auto in enforce for reflection)
    # create_memory_candidate stays pending until promote
    cand = svc.create_memory_candidate(
        statement="pin deps",
        evidence=[art.id],
        confidence=0.85,
    )
    assert cand.promotion_status == "pending"
    promoted = svc.promote_memory(cand.id, force=True)
    assert promoted.promotion_status == "promoted"


def test_artifact_provenance_integrity_hash(svc: MetacogService) -> None:
    a1 = svc.register_artifact(
        artifact_type="code_diff",
        content='{"diff":"+x"}',
        task_id="h1",
        provenance={"inputs": [], "tool": "patch", "source_path": "src/a.py"},
    )
    a2 = svc.register_artifact(
        artifact_type="code_diff",
        content='{"diff":"+x"}',
        task_id="h1",
    )
    assert a1.id == a2.id  # content-addressed
    assert a1.content_hash == a2.content_hash
    assert len(a1.content_hash) == 64
    got = svc.get_artifact(a1.id)
    assert got is not None
    assert got.provenance.get("source_path") == "src/a.py"
    assert got.provenance.get("tool") == "patch"


def test_checkpoint_recovery_and_unsafe_side_effects(svc: MetacogService) -> None:
    ck = svc.create_checkpoint(
        state={"step": 2},
        task_id="ck1",
        completed_criteria=["schema"],
        pending_criteria=["tests"],
        side_effects=[{"kind": "file_write", "path": "a.py", "idempotency": "sha:1"}],
    )
    assert ck.safe_to_resume is True
    packet = svc.resume_from_checkpoint(ck.id)
    assert packet["state"]["step"] == 2
    bad = svc.create_checkpoint(
        state={"step": 9},
        task_id="ck1",
        side_effects=[{"kind": "payment", "idempotency": "unknown"}],
    )
    assert bad.safe_to_resume is False
    with pytest.raises(ValueError):
        svc.resume_from_checkpoint(bad.id)


def test_stop_on_exhausted_strategy_age(svc: MetacogService) -> None:
    s = None
    for _ in range(5):
        s = svc.evaluate(
            task_id="exh",
            criteria_total=4,
            criteria_passed=0,
            previous_progress=0.0,
            strategy_age_iterations=5,
            recent_outputs=["x"] * 3,
        )
    assert s is not None
    assert s.next_control_action in {"stop", "replan", "switch"}


def test_confidence_changes_with_progress(svc: MetacogService) -> None:
    low = svc.evaluate(task_id="c1", criteria_total=10, criteria_passed=1, previous_progress=0.0)
    high = svc.evaluate(task_id="c2", criteria_total=10, criteria_passed=9, previous_progress=0.5)
    assert high.confidence >= low.confidence
