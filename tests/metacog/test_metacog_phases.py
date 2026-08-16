"""End-to-end unit coverage for metacog Phases 0–8 (LIVE/enforce default)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.metacog.config import (
    clear_metacog_config_cache,
    memory_promotion_mode,
    metacog_mode,
    skill_canary_mode,
    strategy_switch_mode,
)
from omniagentos.metacog.service import MetacogService
from omniagentos.metacog.store import MetacogStore


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetacogService:
    monkeypatch.setenv("OMNIAGENTOS_METACOG_ARTIFACTS_ROOT", str(tmp_path / "arts"))
    # LIVE defaults for product; tests that need shadow set env explicitly.
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MODE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MEMORY_PROMOTION", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_METACOG_STRATEGY_SWITCH", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_METACOG_SKILL_CANARY", raising=False)
    clear_metacog_config_cache()
    store = MetacogStore(str(tmp_path / "metacog.db"))
    return MetacogService(store=store)


# --- Phase 0 ---------------------------------------------------------------


def test_phase0_defaults_are_live_enforce(svc: MetacogService) -> None:
    health = svc.health()
    assert health["ok"] is True
    # Control plane LIVE (enforce) for mode/strategy; memory promotion is now
    # enforced via promote_memory guards (HANDOFF LANE B).
    assert metacog_mode() == "enforce"
    assert memory_promotion_mode() == "enforce"
    assert strategy_switch_mode() == "enforce"
    assert skill_canary_mode() == "canary"
    assert health["mode"] == "enforce"
    assert "solo_executor" in health["strategies"]


# --- Phase 1 ---------------------------------------------------------------


def test_phase1_artifact_register_dedupes_by_hash(svc: MetacogService) -> None:
    a1 = svc.register_artifact(
        artifact_type="code_diff",
        content='{"diff":"+print(1)"}',
        task_id="task_1",
        run_id="run_1",
        provenance={"inputs": []},
    )
    a2 = svc.register_artifact(
        artifact_type="code_diff",
        content='{"diff":"+print(1)"}',
        task_id="task_1",
        run_id="run_1",
    )
    assert a1.id == a2.id
    assert a1.content_hash == a2.content_hash
    got = svc.get_artifact(a1.id)
    assert got is not None
    assert got.artifact_type == "code_diff"


def test_phase1_checkpoint_resume_and_unsafe_side_effects(svc: MetacogService) -> None:
    safe = svc.create_checkpoint(
        state={"step": 3, "cursor": "src/a.py"},
        task_id="task_ck",
        completed_criteria=["schema"],
        pending_criteria=["tests"],
        side_effects=[{"kind": "file_write", "path": "src/a.py", "idempotency": "sha:abc"}],
        artifact_ids=[],
    )
    assert safe.safe_to_resume is True
    packet = svc.resume_from_checkpoint(safe.id)
    assert packet["state"]["step"] == 3
    assert packet["completed_criteria"] == ["schema"]

    unsafe = svc.create_checkpoint(
        state={"step": 9},
        task_id="task_ck",
        side_effects=[{"kind": "payment", "idempotency": "unknown"}],
    )
    assert unsafe.safe_to_resume is False
    assert unsafe.replay_risk == "high"
    with pytest.raises(ValueError, match="not safe"):
        svc.resume_from_checkpoint(unsafe.id)


# --- Phase 2 ---------------------------------------------------------------


def test_phase2_context_compiler_keeps_criteria(svc: MetacogService) -> None:
    art = svc.register_artifact(
        artifact_type="verification_report",
        content='{"verdict":"pending"}',
        task_id="task_ctx",
    )
    packet = svc.compile_context(
        task_id="task_ctx",
        objective="Register artifacts with SHA-256 dedupe",
        acceptance_criteria=[
            "identical content reuses canonical artifact",
            "provenance edges append-only",
        ],
    )
    assert "Acceptance criteria" in packet.block
    assert "identical content" in packet.block
    assert art.id in packet.artifact_ids
    assert packet.budget_tokens > 0


def test_phase2_context_compiler_defaults_to_promoted_memories(
    svc: MetacogService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker contexts stay promotion-gated while the operator route can inspect shadow."""
    from omniagentos.api.routes import metacog as metacog_routes
    from omniagentos.api.routes.metacog import CompileContextBody
    from omniagentos.metacog.contracts import MemoryRecord

    promoted = svc.store.upsert_memory(
        MemoryRecord(statement="Retry requests with bounded backoff", promotion_status="promoted")
    )
    shadow = svc.store.upsert_memory(
        MemoryRecord(statement="Retry requests with experimental jitter", promotion_status="shadow")
    )
    unknown = svc.store.upsert_memory(
        MemoryRecord(statement="Retry requests with unknown policy", promotion_status="pending")
    )
    # The schema rejects unknown values outright; the promoted-only query would also
    # exclude one if an external/legacy database somehow contained it.
    with pytest.raises(sqlite3.IntegrityError):
        svc.store._connection.execute(
            "UPDATE metacog_memory_records SET promotion_status = 'unknown' WHERE id = ?",
            (unknown.id,),
        )

    worker_packet = svc.compile_context(objective="Retry requests")
    assert worker_packet.memory_ids == [promoted.id]
    assert shadow.id not in worker_packet.memory_ids
    assert unknown.id not in worker_packet.memory_ids

    operator_packet = svc.compile_context(objective="Retry requests", include_shadow=True)
    assert set(operator_packet.memory_ids) == {promoted.id, shadow.id}
    assert unknown.id not in operator_packet.memory_ids

    monkeypatch.setattr(metacog_routes, "_SERVICE", svc)
    route_packet = metacog_routes.compile_context(CompileContextBody(objective="Retry requests"))
    assert set(route_packet["memory_ids"]) == {promoted.id, shadow.id}


# --- Phase 3 ---------------------------------------------------------------


def test_phase3_memory_requires_evidence_and_live_promotes(
    svc: MetacogService, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="evidence"):
        svc.create_memory_candidate(statement="x", evidence=[])

    art = svc.register_artifact(
        artifact_type="test_log",
        content="FAILED assert foo",
        task_id="task_mem",
    )
    mem = svc.create_memory_candidate(
        statement="Always pin base SHA before worktree commits",
        evidence=[art.id],
        confidence=0.8,
    )
    assert mem.promotion_status == "pending"

    # Default memory_promotion=enforce → promote records promoted
    out = svc.promote_memory(mem.id)
    assert out.promotion_status == "promoted"
    out_live = svc.promote_memory(mem.id, force=True)
    assert out_live.promotion_status == "promoted"

    # Shadow mode still available when configured
    monkeypatch.setenv("OMNIAGENTOS_METACOG_MEMORY_PROMOTION", "shadow")
    clear_metacog_config_cache()
    art2 = svc.register_artifact(artifact_type="test_log", content="other", task_id="task_mem2")
    mem2 = svc.create_memory_candidate(
        statement="shadow-only lesson", evidence=[art2.id], confidence=0.6
    )
    out_shadow = svc.promote_memory(mem2.id)
    assert out_shadow.promotion_status == "shadow"
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MEMORY_PROMOTION", raising=False)
    clear_metacog_config_cache()

    # Invalidation down-ranks
    inv = svc.invalidate_memory(mem.id, reason="file changed", file_paths=["src/a.py"])
    assert inv.invalidated_by.get("reason") == "file changed"
    assert inv.confidence <= 0.2


def test_phase3_stale_memory_not_retrieved_after_invalidation(svc: MetacogService) -> None:
    art = svc.register_artifact(artifact_type="note", content="old advice", task_id="t")
    mem = svc.create_memory_candidate(
        statement="use sqlite busy_timeout forever",
        evidence=[art.id],
        confidence=0.9,
    )
    svc.promote_memory(mem.id, force=True)
    svc.invalidate_memory(mem.id, reason="stale")
    hits = svc.search_memory("sqlite busy_timeout")
    assert all(h.id != mem.id for h in hits)


# --- Phase 4 ---------------------------------------------------------------


def test_phase4_two_no_progress_cycles_refuse_continue(svc: MetacogService) -> None:
    # First evaluation: progress 0
    svc.evaluate(
        task_id="task_stall",
        criteria_total=4,
        criteria_passed=0,
        previous_progress=0.0,
        recent_outputs=["trying A", "trying A"],
    )
    # Drive consecutive no-progress
    svc.evaluate(
        task_id="task_stall",
        criteria_total=4,
        criteria_passed=0,
        previous_progress=0.0,
        strategy_age_iterations=1,
        recent_outputs=["trying A", "trying A", "trying A"],
    )
    s3 = svc.evaluate(
        task_id="task_stall",
        criteria_total=4,
        criteria_passed=0,
        previous_progress=0.0,
        strategy_age_iterations=2,
        recent_outputs=["trying A"] * 4,
    )
    # After enough no-progress, must not CONTINUE
    assert s3.next_control_action != "continue"
    assert (
        s3.stall_count >= 2
        or "no_progress" in " ".join(s3.reason_codes)
        or s3.next_control_action
        in {
            "replan",
            "stop",
            "switch",
        }
    )
    # Progress improvement allows continue-ish path
    s_ok = svc.evaluate(
        task_id="task_ok",
        criteria_total=4,
        criteria_passed=3,
        previous_progress=0.25,
    )
    assert s_ok.objective_progress == 0.75
    assert s_ok.quality_trend == "improving"


# --- Phase 5 ---------------------------------------------------------------


def test_phase5_strategy_select_and_live_switch(
    svc: MetacogService, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = svc.select_strategy(domain="coding", novel=True)
    assert s.id == "hypothesis_portfolio"
    s2 = svc.select_strategy(prefer_verifier=True)
    assert s2.requires_verifier is True

    # LIVE enforce → switch is applied
    result = svc.switch_strategy(
        from_strategy="solo_executor",
        to_strategy="generator_critic",
        task_id="task_sw",
        reason_codes=["stall"],
    )
    assert result["to_strategy"] == "generator_critic"
    assert result["applied"] is True
    assert result["mode"] == "enforce"

    # Shadow still works when forced via env
    monkeypatch.setenv("OMNIAGENTOS_METACOG_STRATEGY_SWITCH", "shadow")
    clear_metacog_config_cache()
    shadowed = svc.switch_strategy(
        from_strategy="generator_critic",
        to_strategy="repair_loop",
    )
    assert shadowed["applied"] is False
    assert shadowed["mode"] == "shadow"
    monkeypatch.delenv("OMNIAGENTOS_METACOG_STRATEGY_SWITCH", raising=False)
    clear_metacog_config_cache()


# --- Phase 6 ---------------------------------------------------------------


def test_phase6_reflection_requires_evidence_for_candidates(svc: MetacogService) -> None:
    report = svc.reflect(
        outcome="failed",
        task_id="task_r",
        failure_summary="assert False in test_foo",
        evidence_artifact_ids=[],
    )
    assert report.candidate_ids == []
    assert report.report.get("candidate_skipped") == "no_evidence"

    art = svc.register_artifact(
        artifact_type="test_log",
        content="Traceback: AssertionError",
        task_id="task_r",
    )
    report2 = svc.reflect(
        outcome="failed",
        task_id="task_r",
        failure_summary="assert False in test_foo",
        evidence_artifact_ids=[art.id],
    )
    assert len(report2.candidate_ids) == 1
    mem = svc.store.get_memory(report2.candidate_ids[0])
    assert mem is not None
    # Reflection routes through promote_memory; shadow default keeps candidates
    # as shadow/pending rather than silent durable promote.
    assert mem.promotion_status in {"shadow", "pending", "promoted"}


def test_phase6_skill_synthesis_needs_recurrence(svc: MetacogService) -> None:
    art = svc.register_artifact(artifact_type="proc", content="steps...", task_id="t")
    mem = svc.create_memory_candidate(
        statement="Run tests before commit",
        evidence=[art.id],
        memory_type="procedure",
    )
    mem.promotion_status = "shadow"
    mem.sample_count = 1
    svc.store.upsert_memory(mem)
    with pytest.raises(ValueError, match="recurrence"):
        svc.synthesize_skill(skill_slug="run-tests-first", memory_ids=[mem.id])

    mem.sample_count = 3
    mem.promotion_status = "promoted"
    svc.store.upsert_memory(mem)
    skill = svc.synthesize_skill(skill_slug="run-tests-first", memory_ids=[mem.id], force=True)
    assert skill.skill_slug == "run-tests-first"
    assert skill.status in {"shadow", "canary", "promoted"}


# --- Phase 7–8 -------------------------------------------------------------


def test_phase7_coding_fingerprint_stable(svc: MetacogService) -> None:
    a = svc.coding_task_fingerprint(
        objective="Add artifact registration",
        write_set=["omniagentos/artifacts/**"],
        repo="OmniAgentOS",
    )
    b = svc.coding_task_fingerprint(
        objective="Add artifact registration",
        write_set=["omniagentos/artifacts/**"],
        repo="OmniAgentOS",
    )
    c = svc.coding_task_fingerprint(
        objective="Different objective",
        write_set=["omniagentos/artifacts/**"],
        repo="OmniAgentOS",
    )
    assert a == b
    assert a != c


def test_phase8_env_can_force_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIAGENTOS_METACOG_MODE", "off")
    monkeypatch.setenv("OMNIAGENTOS_METACOG_STRATEGY_SWITCH", "off")
    clear_metacog_config_cache()
    assert metacog_mode() == "off"
    assert strategy_switch_mode() == "off"
    store = MetacogStore(str(tmp_path / "x.db"))
    svc = MetacogService(store=store)
    with pytest.raises(PermissionError):
        svc.switch_strategy(from_strategy="solo_executor", to_strategy="repair_loop")
