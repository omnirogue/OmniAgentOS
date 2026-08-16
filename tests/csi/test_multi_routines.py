"""All implemented CSI routines can compile evidence and run offline."""

from __future__ import annotations

from pathlib import Path

from omniagentos.csi.config import CsiConfig, RoutineSpec, clear_csi_config_cache
from omniagentos.csi.engine import CsiEngine
from omniagentos.csi.evidence import IMPLEMENTED_ROUTINES, compile_packet


def _cfg() -> CsiConfig:
    routines = {
        rid: RoutineSpec(
            id=rid,
            window_days=7,
            planners=("grok", "sol"),
            enabled=True,
        )
        for rid in sorted(IMPLEMENTED_ROUTINES)
    }
    return CsiConfig(
        enabled=True,
        global_halt=False,
        min_panel_size=2,
        use_live_planners=False,
        routines=routines,
    )


def test_all_routines_compile_packet() -> None:
    for rid in sorted(IMPLEMENTED_ROUTINES):
        pkt = compile_packet(rid, config=_cfg())
        assert pkt.routine == rid
        assert pkt.frozen_surfaces


def test_design_and_three_peers_run(tmp_path: Path) -> None:
    clear_csi_config_cache()
    engine = CsiEngine(config=_cfg(), db_path=tmp_path / "m.db")
    for rid in ("design", "self_learning", "routing", "skills"):
        r = engine.run_routine(rid)
        assert r.run_id.startswith("csi_"), rid
        assert r.status in {
            "NO_CHANGE",
            "AWAITING_HUMAN",
            "DEFERRED",
            "INCIDENT",
        }, (rid, r.status, r.no_change_reason)


def test_run_all_eight_implemented(tmp_path: Path) -> None:
    engine = CsiEngine(config=_cfg(), db_path=tmp_path / "all.db")
    statuses = []
    for rid in sorted(IMPLEMENTED_ROUTINES):
        r = engine.run_routine(rid)
        statuses.append((rid, r.status, r.verdict))
        assert r.verdict != "halted" or "not_implemented" not in (r.no_change_reason or "")
    assert len(statuses) == 8
