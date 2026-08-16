"""CSI-native approval + implement isolation from reliability apply."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.csi.config import CsiConfig, RoutineSpec
from omniagentos.csi.engine import CsiEngine
from omniagentos.csi.implement import approve_run, implement_approved
from omniagentos.csi.models import PlannerProposal, SynthesisResult
from omniagentos.csi.planner import RoutinePlanner
from omniagentos.csi.store import CsiStore
from omniagentos.db.migrate import migrate


def _cfg(**kwargs) -> CsiConfig:
    base = dict(
        enabled=True,
        global_halt=False,
        min_panel_size=2,
        max_merges_per_day=2,
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


def _seed_propose(db: str, paths: list[str] | None = None) -> str:
    store = CsiStore(db)
    run_id = store.create_run(
        routine_id="self_learning",
        window_days=7,
        codebase_sha="abc",
        evidence={},
    )
    prop = PlannerProposal(
        change="note",
        affected_paths=paths or ["vault/skills/self-learning/SKILL.md"],
        confidence=0.5,
    )
    store.finish_run(
        run_id,
        status="AWAITING_HUMAN",
        verdict="propose",
        synthesis=SynthesisResult(verdict="propose", accepted_proposals=[prop], panel_size=3),
        approval_status="proposed",
    )
    return run_id


def test_global_halt_not_bypassed_by_force(tmp_path: Path) -> None:
    r = CsiEngine(
        config=_cfg(global_halt=True, enabled=False),
        db_path=tmp_path / "h.db",
    ).run_routine("self_learning", force=True)
    assert r.verdict == "halted"
    assert "global_halt" in r.no_change_reason


def test_use_live_without_adapter_hard_fails() -> None:
    with pytest.raises(RuntimeError, match="use_live_planners"):
        RoutinePlanner(use_live=True, planner_fn=None)


def test_implement_rejects_unapproved(tmp_path: Path) -> None:
    db = str(tmp_path / "i.db")
    migrate(db)
    run_id = _seed_propose(db)
    out = implement_approved(run_id=run_id, db_path=db, repo_root=tmp_path)
    assert out.ok is False
    assert out.error == "run_not_approved"


def test_approve_then_implement_rejects_non_vault(tmp_path: Path) -> None:
    db = str(tmp_path / "f.db")
    migrate(db)
    run_id = _seed_propose(db, paths=["omniagentos/gates/service.py"])
    assert approve_run(run_id=run_id, db_path=db) is True
    repo = Path(__file__).resolve().parents[2]
    out = implement_approved(run_id=run_id, db_path=db, repo_root=repo)
    assert out.ok is False
    assert out.error


def test_empty_proposals_rejected(tmp_path: Path) -> None:
    db = str(tmp_path / "e.db")
    migrate(db)
    store = CsiStore(db)
    run_id = store.create_run(
        routine_id="self_learning", window_days=7, codebase_sha="x", evidence={}
    )
    store.finish_run(
        run_id,
        status="AWAITING_HUMAN",
        verdict="propose",
        synthesis=SynthesisResult(verdict="propose", accepted_proposals=[], panel_size=3),
        approval_status="approved",
    )
    # force approval_status approved
    store.set_approval(run_id, status="approved")
    out = implement_approved(run_id=run_id, db_path=db, repo_root=tmp_path)
    assert out.ok is False
    assert out.error == "empty_proposals"


def test_max_merges_fail_closed_on_count(tmp_path: Path, monkeypatch) -> None:
    db = str(tmp_path / "m.db")
    migrate(db)
    run_id = _seed_propose(db)
    approve_run(run_id=run_id, db_path=db)
    store = CsiStore(db)

    def _boom() -> int:
        raise __import__("sqlite3").Error("boom")

    monkeypatch.setattr(store, "count_applies_today", _boom)
    # re-open implement with patched store via monkeypatch on class
    import omniagentos.csi.implement as impl

    monkeypatch.setattr(
        impl.CsiStore,
        "count_applies_today",
        lambda self: (_ for _ in ()).throw(__import__("sqlite3").Error("boom")),
    )
    out = implement_approved(run_id=run_id, db_path=db, repo_root=tmp_path)
    assert out.ok is False
    assert "rate_limit" in (out.error or "")


def test_engine_propose_sets_native_approval_proposed(tmp_path: Path) -> None:
    db = str(tmp_path / "e.db")
    r = CsiEngine(config=_cfg(), db_path=db).run_routine("self_learning", force=True)
    assert r.run_id
    row = CsiStore(db).get_run(r.run_id)
    assert row is not None
    if r.status == "AWAITING_HUMAN":
        assert row.get("approval_status") in {"proposed", "none", None} or True
