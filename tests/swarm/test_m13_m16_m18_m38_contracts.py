"""Execution tests for L16 contract slice M-13, M-16, M-18, M-38.

These are behavioral tests (not source greps): they drive real provision/spawn/
fan-in/pipeline paths and assert observable side effects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.cbm.service import CognitiveBudgetService
from omniagentos.collab.store import CollabStore
from omniagentos.fanin import Candidate, FanInMode, run_fanin_pipeline
from omniagentos.graph_runtime.service import GraphRuntimeService
from omniagentos.swarm.cbm_wiring import (
    close_task_allocation,
    derive_cbm_inputs,
    guard_control_action,
)
from omniagentos.swarm.contracts import SwarmPlan, SwarmTaskSpec
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.graph_soak import graph_runtime_mode, maybe_link_graph_for_swarm
from omniagentos.swarm.planner import provision_run
from omniagentos.swarm.scheduler import SpawnRequest
from omniagentos.swarm.spawn import UnifiedSpawner
from omniagentos.taskcontract import (
    AcceptanceCriterion,
    ContractState,
    RiskClass,
    TaskContract,
    TaskContractError,
    TaskContractStore,
    can_transition,
    validate_transition,
)

# ---------------------------------------------------------------------------
# M-13 — observable gated Graph default-path soak
# ---------------------------------------------------------------------------


def test_graph_soak_default_mode_is_shadow_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIAGENTOS_GRAPH_RUNTIME", raising=False)
    assert graph_runtime_mode({}) == "shadow"
    assert graph_runtime_mode({"OMNIAGENTOS_GRAPH_RUNTIME": ""}) == "shadow"
    assert graph_runtime_mode({"OMNIAGENTOS_GRAPH_RUNTIME": "0"}) == "off"
    assert graph_runtime_mode({"OMNIAGENTOS_GRAPH_RUNTIME": "1"}) == "on"
    assert graph_runtime_mode({"OMNIAGENTOS_GRAPH_RUNTIME": "soak"}) == "soak"


def test_graph_default_path_soak_provisions_observable_diamond(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset flag → multi-task provision links a diamond under soak claim."""
    monkeypatch.delenv("OMNIAGENTOS_GRAPH_RUNTIME", raising=False)
    db = str(tmp_path / "graph-soak.db")
    CollabStore(db)
    dal = SwarmDal(db)
    plan = SwarmPlan(
        goal="Default path soak",
        mode="swarm",
        target_n=2,
        tasks=[
            SwarmTaskSpec(
                id="t1",
                title="A",
                description="fan a",
                depends_on=[],
                owned_paths=["a.py"],
                acceptance="a ok",
            ),
            SwarmTaskSpec(
                id="t2",
                title="B",
                description="fan b",
                depends_on=[],
                owned_paths=["b.py"],
                acceptance="b ok",
            ),
            SwarmTaskSpec(
                id="integration",
                title="Integrate",
                description="merge",
                depends_on=["t1", "t2"],
                owned_paths=["out.md"],
                acceptance="merged",
            ),
        ],
        integration_task_id="integration",
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    out = provision_run(plan, dal=dal, working_dir=str(ws), write_plan_doc=False)

    soak = out.get("graph_soak")
    assert isinstance(soak, dict)
    assert soak["enabled"] is True
    assert soak["mode"] in {"shadow", "soak"}
    # Explicitly do not claim the graph is the completed default executor.
    assert soak["claim"] == "linked_for_soak"
    assert soak["metadata"].get("default_executor") is False
    graph_id = out.get("graph_run_id") or soak.get("graph_run_id")
    assert graph_id
    gr = GraphRuntimeService(db_path=db)
    run = gr.get_run(str(graph_id))
    assert run is not None
    assert run.get("swarm_run_id") == out["run"]["id"]
    # Soak must not auto-complete the graph.
    assert run.get("status") != "completed"


def test_graph_soak_helper_skips_single_task(tmp_path: Path) -> None:
    db = str(tmp_path / "g.db")
    CollabStore(db)
    obs = maybe_link_graph_for_swarm(
        db_path=db,
        swarm_run_id="swr_one",
        project_id=None,
        target_n=1,
        env={},  # default shadow, but target_n blocks
    )
    assert obs.enabled is False
    assert obs.graph_run_id is None


# ---------------------------------------------------------------------------
# M-16 — authoritative persisted TaskContract transitions
# ---------------------------------------------------------------------------


def _contract(**overrides: Any) -> TaskContract:
    base: dict[str, Any] = {
        "objective": "Ship fan-in package",
        "acceptance_criteria": [
            AcceptanceCriterion(id="tests", condition="pytest green").to_dict()
        ],
        "read_set": ["omniagentos/fanin"],
        "write_set": ["omniagentos/fanin"],
        "risk_class": RiskClass.R1,
        "task_id": "t_demo",
        "lineage_id": "lin_demo",
    }
    base.update(overrides)
    if "acceptance_criteria" in overrides and overrides["acceptance_criteria"]:
        # allow raw criterion objects
        crit = overrides["acceptance_criteria"]
        if crit and hasattr(crit[0], "to_dict"):
            base["acceptance_criteria"] = [c.to_dict() for c in crit]
    return TaskContract.from_dict(base)


def test_task_contract_transition_table_fail_closed() -> None:
    assert can_transition(ContractState.DRAFT, ContractState.ADOPTED)
    assert can_transition(ContractState.ACTIVE, ContractState.VERIFYING)
    assert not can_transition(ContractState.CLOSED, ContractState.ACTIVE)
    with pytest.raises(TaskContractError, match="illegal"):
        validate_transition(ContractState.CLOSED, ContractState.ACTIVE)


def test_task_contract_store_adopt_and_transition_with_ledger_hash(tmp_path: Path) -> None:
    db = str(tmp_path / "contracts.db")
    store = TaskContractStore(database=db)
    try:
        tc = _contract()
        digest = tc.contract_hash()
        rec = store.adopt(tc, lane="swarm", ref_id="task_1", actor="test")
        assert rec.contract_hash == digest
        assert rec.state == ContractState.ADOPTED.value

        # Ledger identity check rejects mismatched expected_hash
        with pytest.raises(TaskContractError, match="hash mismatch"):
            store.transition(
                rec.id,
                ContractState.ACTIVE,
                expected_hash="deadbeef",
            )

        active = store.transition(
            rec.id,
            ContractState.ACTIVE,
            reason="spawn",
            expected_hash=digest,
        )
        assert active.state == ContractState.ACTIVE.value
        assert store.assert_hash_identity(rec.id) == digest

        store.transition(rec.id, ContractState.VERIFYING, reason="settle")
        store.transition(rec.id, ContractState.VERIFIED, reason="pass")
        closed = store.transition(rec.id, ContractState.CLOSED, reason="done")
        assert closed.state == ContractState.CLOSED.value

        txs = store.list_transitions(rec.id)
        assert len(txs) >= 4
        assert txs[0]["to_state"] == ContractState.ADOPTED.value
        # Illegal reopen after close
        with pytest.raises(TaskContractError, match="illegal"):
            store.transition(rec.id, ContractState.ACTIVE, reason="nope")
    finally:
        store.close()


def test_task_contract_adopt_is_idempotent_on_hash_lane_ref(tmp_path: Path) -> None:
    db = str(tmp_path / "c2.db")
    store = TaskContractStore(database=db)
    try:
        tc = _contract()
        a = store.adopt(tc, lane="swarm", ref_id="t1")
        b = store.adopt(tc, lane="swarm", ref_id="t1")
        assert a.id == b.id
        assert a.contract_hash == b.contract_hash
    finally:
        store.close()


# ---------------------------------------------------------------------------
# M-18 — executable fan-in + verifier + context + domain pack + learning
# ---------------------------------------------------------------------------


def test_fanin_pipeline_end_to_end_with_verify_and_learning(tmp_path: Path) -> None:
    learning = tmp_path / "learning.jsonl"
    # Brand/domain pack
    pack = tmp_path / "brand"
    pack.mkdir()
    (pack / "voice.md").write_text("Be concise.\n", encoding="utf-8")
    (pack / "offer.json").write_text(json.dumps({"product": "Omni"}), encoding="utf-8")
    (pack / "banned_claims.txt").write_text("# none\n", encoding="utf-8")

    candidates = [
        Candidate(id="a", content={"answer": "1"}, score=0.4, confidence=0.5),
        Candidate(id="b", content={"answer": "2"}, score=0.9, confidence=0.8),
    ]
    result = run_fanin_pipeline(
        candidates,
        mode=FanInMode.ADJUDICATE,
        criteria={"min_score": 0.5},
        context_layers=[("goal", "pick best answer"), ("acceptance", "score>=0.5")],
        domain_pack=pack,
        scope_root=tmp_path,
        scope="fanin-test",
        learning_path=learning,
    )
    assert "compile_context" in result.stages
    assert "domain_pack" in result.stages
    assert "adjudicate" in result.stages
    assert "verify" in result.stages
    assert "learning_exit" in result.stages
    assert result.context_fingerprint
    assert result.domain_pack_path
    assert result.fanin.selected and result.fanin.selected[0].id == "b"
    assert result.verify is not None and result.verify.ok is True
    assert result.needs_replan is False
    assert result.learning_exit == "accepted"
    assert result.learning_decision_id
    # Learning log was written
    lines = learning.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 2
    kinds = {json.loads(line)["kind"] for line in lines}
    assert "decision" in kinds
    assert "outcome" in kinds


def test_fanin_pipeline_verify_fail_forces_replan_not_false_accept(tmp_path: Path) -> None:
    from omniagentos.fanin.pipeline import VerifyOutcome

    learning = tmp_path / "l2.jsonl"
    candidates = [
        Candidate(id="weak", content={"x": 1}, score=0.95, confidence=0.9),
    ]
    result = run_fanin_pipeline(
        candidates,
        criteria={"min_score": 0.1},
        learning_path=learning,
        verify=lambda selected, evidence: VerifyOutcome(
            ok=False, decision="deny", evidence={"reason": "mechanical_fail"}
        ),
    )
    assert result.fanin.selected
    assert result.verify is not None and result.verify.ok is False
    assert result.needs_replan is True
    assert result.learning_exit == "replan"


# ---------------------------------------------------------------------------
# M-38 — real CBM inputs, close allocation, no false replans
# ---------------------------------------------------------------------------


def test_derive_cbm_inputs_uses_task_signals_not_constants() -> None:
    low = derive_cbm_inputs(
        task={"title": "Rename variable", "complexity": "simple", "est_agent_minutes": 5},
        swarm_json={"risk_class": "none", "acceptance": "renamed", "verify_command": "pytest"},
    )
    high = derive_cbm_inputs(
        task={
            "title": "Architect distributed security migration",
            "description": "novel greenfield protocol redesign",
            "complexity": "complex",
            "est_agent_minutes": 90,
            "risk_class": "destructive",
        },
        swarm_json={"risk_class": "destructive"},
    )
    assert low.difficulty in {"low", "medium"}
    assert high.difficulty == "high"
    assert high.novelty == "high"
    assert high.risk_class == "irreversible"
    assert high.required_quality >= low.required_quality
    assert high.fingerprint["source"] == "l16.derive_cbm_inputs"
    assert high.fingerprint["has_acceptance"] is False


def test_spawn_persists_real_cbm_inputs_and_closes(tmp_path: Path) -> None:
    """M-38 execution: allocate with real inputs, stamp swarm_json, close."""

    class _Supervisor:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def spawn(self, **kwargs: Any) -> str:
            self.calls.append(kwargs)
            return "ses_m38"

    class _Runner:
        def spawn(self, **kwargs: Any) -> str:
            return "ses_p"

    class _SwarmDal:
        def __init__(self) -> None:
            self.tasks: dict[str, dict[str, Any]] = {}
            self.swarm_jsons: dict[str, dict[str, Any]] = {}

        def tasks_for_run(self, run_id: str) -> list[dict[str, Any]]:
            return list(self.tasks.values())

        def get_swarm_json(self, task_id: str) -> dict[str, Any] | None:
            return self.swarm_jsons.get(task_id)

        def set_swarm_json(self, task_id: str, swarm_json: dict[str, Any]) -> bool:
            self.swarm_jsons[task_id] = dict(swarm_json)
            return True

        def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
            return []

    class _SessionsDal:
        def get_session(self, session_id: str) -> dict[str, Any] | None:
            return None

        def set_idle_minutes(self, session_id: str, idle_minutes: float | None) -> bool:
            return True

    db = str(tmp_path / "m38.db")
    dal = _SwarmDal()
    task_id = "t_m38"
    dal.tasks[task_id] = {
        "id": task_id,
        "title": "Architect distributed security migration",
        "description": "novel greenfield protocol redesign",
        "discipline": "coding",
        "priority": "high",
        "complexity": "complex",
        "est_agent_minutes": 90,
        "risk_class": "destructive",
    }
    dal.swarm_jsons[task_id] = {
        "task_key": "hard",
        "risk_class": "destructive",
        "acceptance": "migration green + tests",
        "verify_command": "pytest -q",
        "owned_paths": ["omniagentos/x.py"],
    }
    supervisor = _Supervisor()
    spawner = UnifiedSpawner(
        supervisor=supervisor,
        provider_runner=_Runner(),
        swarm_dal=dal,
        sessions_dal=_SessionsDal(),
        convert_reservation=lambda r, s: True,
        release_reservation=lambda r: True,
        var_root=tmp_path / "var",
        db_path=db,
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    spawner.spawn(
        SpawnRequest(
            run_id="swr_m38",
            task_id=task_id,
            task_key="hard",
            attempt_id="swa_m38",
            working_dir=str(ws),
            prompt="work",
            provider="claude",
            model="sonnet",
            tier="standard",
            account_id="a1",
            idle_minutes=20.0,
            budget_usd_max=5.0,
            reservation_id=None,
            effort="low",  # pre-pin must lose to CBM (H-05)
        )
    )
    stamped = dal.swarm_jsons[task_id]
    assert stamped.get("cbm_allocation_id")
    assert stamped.get("cbm_inputs")
    assert stamped["cbm_inputs"]["difficulty"] == "high"
    assert stamped["cbm_inputs"]["novelty"] == "high"
    assert stamped.get("task_contract_id")
    assert stamped.get("task_contract_hash")
    assert stamped.get("task_contract_state") in {
        ContractState.ADOPTED.value,
        ContractState.ACTIVE.value,
    }
    # Allocation row exists open
    cbm = CognitiveBudgetService(database=db)
    row = cbm.get_allocation(str(stamped["cbm_allocation_id"]))
    assert row is not None
    assert row["status"] in {"active", "escalated"}
    assert int(row["rung"]) >= 2
    # Real fingerprint was stored (not empty decorative {})
    fp = row.get("fingerprint") or {}
    if isinstance(fp, str):
        fp = json.loads(fp)
    assert fp.get("source") == "l16.derive_cbm_inputs" or fp.get("difficulty") == "high"

    closed = close_task_allocation(
        db_path=db,
        swarm_json=stamped,
        first_pass_accepted=True,
        wall_seconds=12.0,
        production_outcome="healthy",
    )
    assert closed is not None
    assert closed.get("closed") is True
    row2 = cbm.get_allocation(str(stamped["cbm_allocation_id"]))
    assert row2 is not None
    assert row2["status"] == "closed"
    cbm.close()


def test_settle_handoff_plain_data_closes_cbm_and_contract(tmp_path: Path) -> None:
    """M-16/M-38 plain-data settle: no scheduler.py import; L10 can call this."""
    from omniagentos.swarm.contract_bridge import adopt_swarm_task_contract
    from omniagentos.swarm.settle_handoff import settle_task_from_swarm_json

    db = str(tmp_path / "settle.db")
    task = {
        "id": "t_settle",
        "title": "Close me",
        "description": "settle path",
        "risk_class": "none",
    }
    sj: dict[str, Any] = {
        "task_key": "s",
        "acceptance": "done",
        "risk_class": "none",
        "owned_paths": ["a.py"],
    }
    # Allocate + stamp
    cbm = CognitiveBudgetService(database=db)
    alloc = cbm.allocate(
        task_id="t_settle",
        stage="execution",
        risk_class="reversible_internal",
        novelty="low",
        difficulty="medium",
    )
    sj["cbm_allocation_id"] = alloc["id"]
    sj["cbm_status"] = "active"
    rec, sj = adopt_swarm_task_contract(db_path=db, task=task, swarm_json=sj, task_id="t_settle")
    assert rec is not None
    assert sj.get("task_contract_id")

    result = settle_task_from_swarm_json(
        db_path=db,
        swarm_json=sj,
        accepted=True,
        production_outcome="healthy",
        wall_seconds=3.0,
    )
    assert result["cbm_close"]["status"] == "ok"
    assert result["contract"]["status"] == "ok"
    assert result["contract"]["state"] == ContractState.CLOSED.value
    row = cbm.get_allocation(str(sj["cbm_allocation_id"]))
    assert row is not None and row["status"] == "closed"
    cbm.close()


def test_guard_control_action_blocks_false_replans() -> None:
    # No stall evidence → replan rewritten to continue
    act, reason = guard_control_action(
        "replan",
        reason_codes=["ok"],
        evidence_delta=0.0,
        stall_count=0,
    )
    assert act == "continue"
    assert reason == "false_replan_no_stall_evidence"

    # Progressing → continue
    act, reason = guard_control_action(
        "replan",
        reason_codes=["no_progress_cycles"],
        evidence_delta=0.2,
        stall_count=2,
    )
    assert act == "continue"
    assert reason == "false_replan_progressing"

    # Real stall with budget → replan allowed
    act, reason = guard_control_action(
        "replan",
        reason_codes=["no_progress_cycles"],
        evidence_delta=0.0,
        stall_count=3,
        max_replans=1,
        replan_budget_used=0,
    )
    assert act == "replan"
    assert reason == "allowed"

    # Budget exhausted → stop
    act, reason = guard_control_action(
        "replan",
        reason_codes=["no_progress_cycles"],
        stall_count=3,
        max_replans=1,
        replan_budget_used=1,
    )
    assert act == "stop"
    assert reason == "replan_budget_exhausted"


def test_settle_handoff_repeat_settlement_is_idempotent(tmp_path: Path) -> None:
    from omniagentos.db.store import _connect, _row
    from omniagentos.swarm.contract_bridge import adopt_swarm_task_contract
    from omniagentos.swarm.settle_handoff import settle_task_from_swarm_json

    db = str(tmp_path / "repeat_settle.db")
    task = {
        "id": "t_repeat",
        "title": "Repeat settle me",
        "description": "idempotency test",
        "risk_class": "none",
    }
    sj: dict[str, Any] = {
        "task_key": "rep",
        "acceptance": "done",
        "risk_class": "none",
        "owned_paths": ["b.py"],
    }
    cbm = CognitiveBudgetService(database=db)
    alloc = cbm.allocate(
        task_id="t_repeat",
        stage="execution",
        risk_class="reversible_internal",
        novelty="low",
        difficulty="medium",
    )
    sj["cbm_allocation_id"] = alloc["id"]
    sj["cbm_status"] = "active"
    rec, sj = adopt_swarm_task_contract(db_path=db, task=task, swarm_json=sj, task_id="t_repeat")
    cbm.close()

    # First settlement
    res1 = settle_task_from_swarm_json(
        db_path=db,
        swarm_json=sj,
        accepted=True,
        production_outcome="healthy",
        wall_seconds=2.0,
    )
    assert res1["cbm_close"]["status"] == "ok"
    assert res1["cbm_close"]["already_closed"] is False
    assert res1["contract"]["status"] == "ok"

    # Repeat settlement with same or stale swarm_json copy
    res2 = settle_task_from_swarm_json(
        db_path=db,
        swarm_json=sj,
        accepted=True,
        production_outcome="healthy",
        wall_seconds=2.0,
    )
    assert res2["cbm_close"]["status"] == "ok"
    assert res2["cbm_close"]["already_closed"] is True
    assert res2["contract"]["status"] == "ok"

    # Verify database: exactly 1 outcome row written, role stats samples=1
    with _connect(db) as conn:
        outcomes = conn.execute(
            "SELECT COUNT(*) FROM cbm_outcomes WHERE allocation_id = ?", (alloc["id"],)
        ).fetchone()[0]
        assert outcomes == 1
        stats = _row(
            conn.execute(
                "SELECT samples, accepts FROM cbm_role_stats WHERE role = ?",
                (alloc.get("model_role"),),
            ).fetchone()
        )
        if stats:
            assert int(stats["samples"]) == 1
            assert int(stats["accepts"]) == 1


def test_g5_local_verify_rejects_caller_self_attestation(tmp_path: Path) -> None:
    from omniagentos.gates.service import GateService

    g5 = GateService().g5_local_verify({"self_attested": True})
    assert g5.decision == "deny"
    assert g5.evidence.get("reason") == "self_attested_verification_rejected"

    # Candidate with self_attested content rejected by fanin default verifier
    candidates = [
        Candidate(
            id="self_attest_cand",
            content={"answer": "42", "self_attested": True},
            score=0.9,
            confidence=0.9,
        )
    ]
    res = run_fanin_pipeline(
        candidates,
        criteria={"min_score": 0.5},
        scope="self-attest-test",
    )
    assert res.verify is not None
    assert res.verify.ok is False
    assert res.verify.decision == "deny"
    assert res.needs_replan is True


def test_guard_control_action_on_metacog_and_fanin_paths_uses_real_evidence(
    tmp_path: Path,
) -> None:
    from omniagentos.metacog.contracts import MetacognitiveState
    from omniagentos.metacog.service import MetacogService

    meta = MetacogService(db_path=str(tmp_path / "metacog.db"))

    # Metacog evaluate with criteria_passed=2, criteria_total=2 -> progress=1.0
    state = meta.evaluate(
        run_id="r_meta",
        task_id="t_meta",
        criteria_passed=2,
        criteria_total=2,
    )
    assert state.next_control_action == "stop"
    assert "criteria_met" in state.reason_codes

    # Save 2 prior stall snapshots for t_meta2 so stall_count reaches 2
    s0 = MetacognitiveState(new_evidence_delta=0.0, next_control_action="continue")
    meta.store.save_snapshot(s0, run_id="r_meta2", task_id="t_meta2")
    meta.store.save_snapshot(s0, run_id="r_meta2", task_id="t_meta2")

    # Metacog evaluate with 0 progress and stall -> replan allowed by guard
    state_stall = meta.evaluate(
        run_id="r_meta2",
        task_id="t_meta2",
        criteria_passed=0,
        criteria_total=2,
        previous_progress=0.0,
    )
    assert state_stall.next_control_action == "replan"
    assert "guard:allowed" in state_stall.reason_codes
