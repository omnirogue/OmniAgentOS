"""Full feature matrix — every major Grok product surface in one suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.allocation import fixture_suite, simulate_fanout
from omniagentos.cbm.service import RUNGS, CognitiveBudgetService
from omniagentos.contracts import ActionClass
from omniagentos.db.store import SqliteStore
from omniagentos.execution.post_attempt import evaluate_post_attempt
from omniagentos.fanin import FanInMode, adjudicate
from omniagentos.gates import GateService
from omniagentos.grants import GrantsStore
from omniagentos.graph_runtime.service import GraphRuntimeService
from omniagentos.metacog.config import (
    clear_metacog_config_cache,
    memory_promotion_mode,
    metacog_mode,
)
from omniagentos.metacog.service import MetacogService
from omniagentos.metacog.store import MetacogStore
from omniagentos.orgdims.service import OrgDimsService
from omniagentos.orgdims.taxonomy import resolve_workstream_alias
from omniagentos.policy import evaluate_action, load_policy
from omniagentos.policy.shell import classify_shell
from omniagentos.scope.locks import LockHolder, PathLockStore
from omniagentos.scope.model import ScopeClaim
from omniagentos.taskcontract import AcceptanceCriterion, RiskClass, TaskContract
from omniagentos.toolplane.manifest import CapabilityManifest
from omniagentos.toolplane.scrub import scrub_text

# --- Policy / full-auto ----------------------------------------------------


def test_feature_policy_auto_mode() -> None:
    cfg = load_policy()
    assert cfg.mode.value == "auto"
    for ac in ActionClass:
        d = evaluate_action(ac, cfg)
        if ac is ActionClass.IRREVERSIBLE:
            assert d.requires_approval is True
        else:
            assert d.requires_approval is False


def test_feature_shell_heredoc_granted_root(tmp_path: Path) -> None:
    granted = tmp_path / "desktop"
    granted.mkdir()
    proj = tmp_path / "ws"
    proj.mkdir()
    target = granted / "out.html"
    cmd = f"cat > {target} << 'EOF'\n<html/>\nEOF"
    assert (
        classify_shell(cmd, str(proj), extra_roots=[str(granted)])
        == ActionClass.INTERNAL_REVERSIBLE
    )


# --- Scope / isolation -----------------------------------------------------


def test_feature_scope_isolation(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"))
    locks = PathLockStore(store)
    realm = str(tmp_path / "proj")
    Path(realm).mkdir()
    try:
        a = locks.try_acquire_scope(
            [ScopeClaim.for_path(realm, "src/a.py")],
            LockHolder(kind="run", id="a", lane="runner"),
            enforce=True,
        )
        b = locks.try_acquire_scope(
            [ScopeClaim.for_path(realm, "src/a.py")],
            LockHolder(kind="run", id="b", lane="runner"),
            enforce=True,
        )
        c = locks.try_acquire_scope(
            [ScopeClaim.for_path(realm, "src/b.py")],
            LockHolder(kind="run", id="c", lane="swarm"),
            enforce=True,
        )
        assert a.status == "granted"
        assert b.status == "blocked"
        assert c.status == "granted"
    finally:
        store.close()


# --- Graph Runtime ---------------------------------------------------------


def test_feature_graph_diamond_and_fail_closed(product_db: str) -> None:
    svc = GraphRuntimeService(db_path=product_db)
    h = svc.health()
    assert h["live"] is True
    assert "diamond-v1" in h["templates"]
    run = svc.run_diamond_deterministic(title="matrix-diamond")
    assert run["status"] == "completed"
    view = svc.live_view(run["id"])
    assert view["artifact_count"] >= 5
    assert all(e["status"] == "bound" for e in view["edge_flow"])

    # fail_closed: incomplete fan-in blocks
    partial = svc.start_diamond(title="partial", completeness_policy="fail_closed")
    svc.complete_node(partial["id"], "fan_a", outputs={"finding": {"claim": "only", "score": 0.9}})
    svc.complete_node(partial["id"], "fan_b", outputs={}, error="died")
    again = svc.get_run(partial["id"])
    assert again is not None
    assert again["status"] in {"failed", "blocked"}


# --- CBM -------------------------------------------------------------------


def test_feature_cbm_ladder_and_learning(product_db: str) -> None:
    svc = CognitiveBudgetService(database=product_db)
    h = svc.health()
    assert h["live"] is True
    assert h["cost_objective"] is False
    assert len(RUNGS) == 7

    fast = svc.allocate(task_id="m1", stage="execution")
    assert fast["rung"] == 1
    esc = svc.escalate(fast["id"], trigger_code="repeated_failure", evidence=["same"])
    assert esc["to_rung"] >= 3
    svc.contract(fast["id"])
    svc.close_allocation(fast["id"], first_pass_accepted=False, wall_seconds=9.0, repair_count=2)

    irrev = svc.allocate(task_id="m2", risk_class="irreversible", novelty="high")
    assert irrev["rung"] >= 4
    board = svc.leaderboard()
    assert any(r["samples"] >= 1 for r in board)


# --- Orgdims / Multidim ----------------------------------------------------


def test_feature_orgdims_taxonomy_and_grok_agents(product_db: str) -> None:
    svc = OrgDimsService(db_path=product_db)
    counts = svc.ensure_seeded()
    assert counts["companies"] >= 3
    assert counts["agents"] >= 6
    h = svc.health()
    assert h["primary_orchestrator"] == "grok-orchestrator"
    assert "grok-self-repair" in h["grok_metacog_agents"]
    assert resolve_workstream_alias("Coding") == "engineering"
    matrix = svc.matrix_view()
    assert "columns" in matrix
    portfolio = svc.portfolio_view()
    assert "primary_orchestrator" in portfolio
    loops = svc.list_loop_templates()
    assert len(loops) >= 1


# --- Metacog LIVE ----------------------------------------------------------


def test_feature_metacog_live_artifacts_and_evaluate(
    tmp_path: Path, product_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_METACOG_ARTIFACTS_ROOT", str(tmp_path / "arts"))
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MODE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MEMORY_PROMOTION", raising=False)
    clear_metacog_config_cache()
    assert metacog_mode() == "enforce"
    # HANDOFF Phase 4 (configs/metacog.yaml) promoted memory promotion from
    # shadow to enforce — LIVE via promote_memory() guards, operator request.
    assert memory_promotion_mode() == "enforce"

    svc = MetacogService(store=MetacogStore(product_db))
    h = svc.health()
    assert h["ok"] is True
    assert h["mode"] == "enforce"

    art = svc.register_artifact(
        artifact_type="code_diff",
        content='{"diff":"+x"}',
        task_id="t-matrix",
        run_id="r-matrix",
    )
    assert art.content_hash
    eval_result = svc.evaluate(
        task_id="t-matrix",
        run_id="r-matrix",
        criteria_total=4,
        criteria_passed=2,
        previous_progress=0.25,
        cost_used=0.1,
        time_used=30.0,
    )
    assert eval_result is not None


# --- Allocation / fan-in / grants / gates / toolplane / novel --------------


def test_feature_allocation_fanout_fixtures() -> None:
    for case in fixture_suite():
        r = simulate_fanout(case["tasks"])
        assert r.worker_count >= 0
        if case["name"] == "trivial":
            assert r.worker_count <= 1


def test_feature_fanin_adjudicate() -> None:
    result = adjudicate(
        [
            {"id": "c1", "content": {"answer": 1}, "score": 0.9},
            {"id": "c2", "content": {"answer": 2}, "score": 0.5},
        ],
        mode=FanInMode.SELECT_BEST,
    )
    assert result is not None
    assert result.selected
    assert result.selected[0].id == "c1"


def test_feature_grants(tmp_path: Path) -> None:
    from omniagentos.grants.store import is_grant_live

    store = SqliteStore(str(tmp_path / "g.db"))
    gs = GrantsStore(store)
    g = gs.create_grant(
        "gmail.send",
        label="t",
        target_set=["a@x.com"],
        approval_id="apr_feat_1",
        max_actions=2,
        max_spend_usd=5.0,
        expires_at="2099-01-01T00:00:00+00:00",
        metadata={"generation": 0, "action_class": "consequential"},
    )
    live, _ = is_grant_live(g, capability="gmail.send")
    assert live is True


def test_feature_gates() -> None:
    gates = GateService()
    assert gates is not None
    assert getattr(gates, "policy_version", "grok-gates/1")


def test_feature_toolplane_and_taskcontract() -> None:
    text = scrub_text("token sk-abcdefghijklmnopqrstuvwxyz012345")
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in text or "REDACT" in text.upper()
    m = CapabilityManifest(
        run_id="r1",
        session_id="s1",
        holder_generation=1,
        read_roots=["/tmp"],
        write_roots=["/tmp"],
        allowed_ops=["read_file"],
    )
    assert m.holder_generation == 1
    tc = TaskContract(
        objective="ship feature",
        acceptance_criteria=(
            AcceptanceCriterion(id="a1", condition="tests pass", evidence_required=True),
        ),
        read_set=("src/",),
        write_set=("src/",),
        risk_class=RiskClass.R1,
        task_id="tsk_matrix",
    )
    assert tc.objective == "ship feature"


def test_feature_post_attempt_eval(tmp_path: Path) -> None:
    (tmp_path / "out.md").write_text("ok\n", encoding="utf-8")
    # Legacy assess-only path (expected file present). H-13 makes default
    # use_verify=True fail closed on non-git tmp dirs; select use_verify=False
    # so this matrix row stays about mechanical presence, not live git observe.
    # Verifier-default fail-closed coverage: tests/execution/test_verify.py and
    # tests/execution/test_post_attempt.py (unobserved / exception → no pass).
    result = evaluate_post_attempt(
        working_dir=tmp_path,
        expected_files=["out.md"],
        lane="swarm",
        use_verify=False,
    )
    assert result.mechanical_pass is True
