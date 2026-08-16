"""Self-Learning routine dry-run (offline planners, no auto-merge)."""

from __future__ import annotations

from pathlib import Path

from omniagentos.csi.config import CsiConfig, RoutineSpec, clear_csi_config_cache
from omniagentos.csi.engine import CsiEngine, run_self_learning
from omniagentos.csi.evidence import compile_self_learning_packet
from omniagentos.csi.frozen import reject_frozen_paths
from omniagentos.csi.models import EvidencePacket, PlannerPlan, PlannerProposal
from omniagentos.csi.planner import offline_planner
from omniagentos.csi.synthesis import PlanSynthesisService


def _cfg(**kwargs) -> CsiConfig:
    base = dict(
        enabled=True,
        global_halt=False,
        min_panel_size=2,
        use_live_planners=False,
        routines={
            "self_learning": RoutineSpec(
                id="self_learning",
                window_days=7,
                planners=("grok", "sol", "kimi"),
                enabled=True,
            )
        },
    )
    base.update(kwargs)
    return CsiConfig(**base)


def test_disabled_halts_without_force(tmp_path: Path) -> None:
    clear_csi_config_cache()
    cfg = _cfg(enabled=False)
    r = CsiEngine(config=cfg, db_path=tmp_path / "c.db").run_routine("self_learning")
    assert r.verdict == "halted"
    assert r.run_id == ""
    assert "enabled" in r.no_change_reason


def test_force_runs_when_disabled(tmp_path: Path) -> None:
    cfg = _cfg(enabled=False)
    r = CsiEngine(config=cfg, db_path=tmp_path / "c.db").run_routine("self_learning", force=True)
    assert r.run_id.startswith("csi_")
    assert r.status in {"NO_CHANGE", "AWAITING_HUMAN", "DEFERRED"}
    # Offline thin evidence → no_change path is the common case
    assert r.verdict in {"no_change", "insufficient_panel", "propose"}


def test_self_learning_packet_compiles() -> None:
    pkt = compile_self_learning_packet(config=_cfg())
    assert pkt.routine == "self_learning"
    assert pkt.frozen_surfaces
    assert "omniagentos/csi/**" in pkt.frozen_surfaces or any(
        "csi" in s for s in pkt.frozen_surfaces
    )


def test_offline_planner_no_change_on_thin_evidence() -> None:
    pkt = EvidencePacket(
        routine="self_learning",
        window_days=7,
        thin_evidence=True,
        evidence_gaps=["no_repeat_failure_signals"],
    )
    plan = offline_planner(pkt, "grok")
    assert plan.verdict == "no_change"


def test_offline_planner_strips_frozen_paths() -> None:
    pkt = EvidencePacket(
        routine="self_learning",
        window_days=7,
        thin_evidence=False,
        repeat_failures=[{"signal": "TypeError", "count": 5}],
    )
    plan = offline_planner(pkt, "sol")
    if plan.verdict == "propose":
        for prop in plan.proposals:
            assert not reject_frozen_paths(prop.affected_paths)


def test_insufficient_panel_single_plan() -> None:
    synth = PlanSynthesisService(min_panel_size=2)
    result = synth.synthesize(
        [
            PlannerPlan(
                routine="self_learning",
                verdict="propose",
                proposals=[
                    PlannerProposal(
                        change="x",
                        affected_paths=["vault/skills/a/SKILL.md"],
                        confidence=0.9,
                    )
                ],
                planner="grok",
                lineage="xai",
            )
        ]
    )
    assert result.verdict == "insufficient_panel"


def test_panel_all_no_change() -> None:
    synth = PlanSynthesisService(min_panel_size=2)
    plans = [
        PlannerPlan(
            routine="self_learning",
            verdict="no_change",
            no_change_reason="thin",
            planner=n,
            lineage=n,
            execution_id=f"request-{n}",
            provenance_verified=True,
        )
        for n in ("grok", "sol", "kimi")
    ]
    result = synth.synthesize(plans)
    assert result.verdict == "no_change"


def test_proposal_with_frozen_path_rejected_in_synthesis() -> None:
    synth = PlanSynthesisService(min_panel_size=2)
    prop = PlannerProposal(
        change="weaken gates",
        affected_paths=["omniagentos/gates/service.py"],
        confidence=0.9,
    )
    plans = [
        PlannerPlan(
            routine="self_learning",
            verdict="propose",
            proposals=[prop],
            planner="grok",
            lineage="xai",
            execution_id="request-grok",
            provenance_verified=True,
        ),
        PlannerPlan(
            routine="self_learning",
            verdict="propose",
            proposals=[prop],
            planner="sol",
            lineage="openai",
            execution_id="request-sol",
            provenance_verified=True,
        ),
    ]
    result = synth.synthesize(plans)
    assert result.verdict == "no_change"
    assert any(r.get("reason") == "frozen_surface" for r in result.rejected_items)


def test_run_self_learning_helper(tmp_path: Path) -> None:
    r = run_self_learning(force=True, db_path=tmp_path / "s.db", config=_cfg(enabled=False))
    assert r.routine_id == "self_learning"
    assert r.run_id


def test_never_auto_merges_status(tmp_path: Path) -> None:
    """Foundation statuses never include COMPLETED/applied without human path."""
    r = CsiEngine(config=_cfg(), db_path=tmp_path / "m.db").run_routine("self_learning")
    assert r.status not in {"COMPLETED", "applying", "applied", "MERGED"}


def test_empty_affected_paths_rejected() -> None:
    synth = PlanSynthesisService(min_panel_size=2)
    empty = PlannerProposal(change="do something", affected_paths=[], confidence=0.9)
    plans = [
        PlannerPlan(
            routine="self_learning",
            verdict="propose",
            proposals=[empty],
            planner="grok",
            lineage="xai",
            execution_id="request-grok",
            provenance_verified=True,
        ),
        PlannerPlan(
            routine="self_learning",
            verdict="propose",
            proposals=[empty],
            planner="sol",
            lineage="openai",
            execution_id="request-sol",
            provenance_verified=True,
        ),
    ]
    result = synth.synthesize(plans)
    assert result.verdict == "no_change"
    assert any(r.get("reason") == "empty_affected_paths" for r in result.rejected_items)
