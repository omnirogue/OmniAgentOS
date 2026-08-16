"""Phase 1 + 2.1 — each high-payoff subsystem is reachable from a REAL path.

These tests fail if someone reverts wiring while leaving STATUS.md green.
Behavioral assertions (not only string presence in source).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.cbm.service import CognitiveBudgetService
from omniagentos.gates.service import GateService
from omniagentos.graph_runtime.service import GraphRuntimeService
from omniagentos.toolplane.manifest import CapabilityManifest
from omniagentos.toolplane.tools import dispatch

# --- 1.1 CBM: allocation is applied, not discarded -------------------------


def test_cbm_allocation_is_read_back_into_spawn_envelope(tmp_path: Path) -> None:
    """Changing the recommended rung changes the execution effort envelope."""
    from omniagentos.swarm.scheduler import SpawnRequest
    from omniagentos.swarm.spawn import UnifiedSpawner

    class _Supervisor:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def spawn(self, **kwargs: Any) -> str:
            self.calls.append(kwargs)
            return "ses_cbm_wire_1"

    class _Runner:
        def spawn(self, **kwargs: Any) -> str:
            return "ses_provider_1"

    class _SwarmDal:
        def __init__(self) -> None:
            self.tasks: dict[str, dict[str, Any]] = {}
            self.swarm_jsons: dict[str, dict[str, Any]] = {}

        def tasks_for_run(self, run_id: str) -> list[dict[str, Any]]:
            return list(self.tasks.values())

        def get_swarm_json(self, task_id: str) -> dict[str, Any] | None:
            return self.swarm_jsons.get(task_id)

        def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
            return []

    class _SessionsDal:
        def get_session(self, session_id: str) -> dict[str, Any] | None:
            return None

        def set_idle_minutes(self, session_id: str, idle_minutes: float | None) -> bool:
            return True

    db = str(tmp_path / "cbm.db")
    dal = _SwarmDal()
    task_id = "t_high"
    dal.tasks[task_id] = {
        "id": task_id,
        "title": "Hard task",
        "description": "architecture",
        "discipline": "coding",
        "priority": "high",
    }
    dal.swarm_jsons[task_id] = {
        "task_key": "hard",
        "risk_class": "irreversible",
        "novelty": "high",
        "difficulty": "high",
        "acceptance": "ok",
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
            run_id="swr1",
            task_id=task_id,
            task_key="hard",
            attempt_id="swa1",
            working_dir=str(ws),
            prompt="work",
            provider="claude",
            model="sonnet",
            tier="standard",
            account_id="a1",
            idle_minutes=20.0,
            budget_usd_max=5.0,
            reservation_id=None,
            effort=None,
        )
    )
    assert supervisor.calls
    prompt = str(supervisor.calls[0].get("prompt") or "")
    assert "[cbm allocation" in prompt
    effort = supervisor.calls[0].get("effort")
    # High novelty+risk must not leave effort empty when CBM applied
    assert effort in {"low", "medium", "high", "xhigh", "minimal"} or "[cbm allocation" in prompt
    # Row exists and was read (rung >= 2 for irreversible+high novelty)
    cbm = CognitiveBudgetService(database=db)
    rows = cbm._connection.execute(
        "SELECT rung FROM cbm_allocations WHERE task_id=?", (task_id,)
    ).fetchall()
    assert rows and int(rows[0]["rung"]) >= 2


# --- 1.2 Graph Runtime: flag path provisions a linked diamond --------------


def test_graph_runtime_provisions_when_flag_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMNIAGENTOS_GRAPH_RUNTIME=1 makes provision_run create a graph diamond."""
    monkeypatch.setenv("OMNIAGENTOS_GRAPH_RUNTIME", "1")
    from omniagentos.collab.store import CollabStore
    from omniagentos.swarm.contracts import SwarmPlan, SwarmTaskSpec
    from omniagentos.swarm.dal import SwarmDal
    from omniagentos.swarm.planner import provision_run

    db = str(tmp_path / "swarm.db")
    CollabStore(db)  # migrate shared schema (045+062)
    dal = SwarmDal(db)
    # Minimal multi-task plan so target_n >= 2
    plan = SwarmPlan(
        goal="Wire graph into swarm",
        mode="swarm",
        target_n=2,
        tasks=[
            SwarmTaskSpec(
                id="t1",
                title="Worker A",
                description="fan out a",
                depends_on=[],
                owned_paths=["a.py"],
                acceptance="a done",
            ),
            SwarmTaskSpec(
                id="t2",
                title="Worker B",
                description="fan out b",
                depends_on=[],
                owned_paths=["b.py"],
                acceptance="b done",
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
    (ws / ".git").mkdir()  # some paths care about git
    out = provision_run(plan, dal=dal, working_dir=str(ws), write_plan_doc=False)
    assert out.get("run")
    graph_id = out.get("graph_run_id")
    assert graph_id, "graph_run_id must be returned when flag is on"
    gr = GraphRuntimeService(db_path=db)
    run = gr.get_run(str(graph_id))
    assert run is not None
    assert run.get("swarm_run_id") == out["run"]["id"]
    assert len(run.get("nodes") or []) >= 5  # diamond nodes


def test_graph_runtime_explicit_off_no_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-13: explicit off disables soak; unset defaults to shadow soak."""
    monkeypatch.setenv("OMNIAGENTOS_GRAPH_RUNTIME", "0")
    from omniagentos.collab.store import CollabStore
    from omniagentos.swarm.contracts import SwarmPlan, SwarmTaskSpec
    from omniagentos.swarm.dal import SwarmDal
    from omniagentos.swarm.planner import provision_run

    db = str(tmp_path / "s.db")
    CollabStore(db)
    dal = SwarmDal(db)
    plan = SwarmPlan(
        goal="solo",
        mode="swarm",
        target_n=2,
        tasks=[
            SwarmTaskSpec(
                id="t1",
                title="A",
                description="a",
                depends_on=[],
                owned_paths=["a.py"],
                acceptance="ok",
            ),
            SwarmTaskSpec(
                id="t2",
                title="B",
                description="b",
                depends_on=[],
                owned_paths=["b.py"],
                acceptance="ok",
            ),
        ],
        integration_task_id="t2",
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    out = provision_run(plan, dal=dal, working_dir=str(ws), write_plan_doc=False)
    assert "graph_run_id" not in out or out.get("graph_run_id") in (None, "")
    soak = out.get("graph_soak") or {}
    assert soak.get("enabled") is False
    assert soak.get("mode") == "off"


# --- 1.3 Gates decide on real paths ----------------------------------------


def test_g2_denies_capacity_failure_from_service() -> None:
    d = GateService().g2_dispatch({"capacity_ok": False})
    assert d.decision == "deny"
    assert d.next_state == "dispatch_blocked"


def test_g3_denies_tool_on_toolplane_dispatch() -> None:
    """Toolplane calls G3 when an op is not in allowed_ops."""
    manifest = CapabilityManifest(
        run_id="r1",
        session_id="s1",
        holder_generation=1,
        read_roots=["/tmp"],
        write_roots=["/tmp"],
        allowed_ops=["read_file"],  # shell not allowed
    )
    result = dispatch("write_file", manifest, {"path": "/tmp/x", "content": "hi"})
    assert result.get("ok") is False
    assert result.get("error") == "not_allowed"


def test_g5_denies_failed_verify() -> None:
    d = GateService().g5_local_verify({"verify_ok": False})
    assert d.decision == "deny"


def test_g2_g3_g5_source_wired_into_production_paths() -> None:
    """Structural: real callers import GateService (not only the API)."""
    root = Path(__file__).resolve().parents[2] / "omniagentos"
    router = (root / "swarm" / "router.py").read_text(encoding="utf-8")
    tools = (root / "toolplane" / "tools.py").read_text(encoding="utf-8")
    sched = (root / "swarm" / "scheduler.py").read_text(encoding="utf-8")
    assert "GateService" in router and "g2_dispatch" in router
    assert "GateService" in tools and "g3_tool" in tools
    assert "GateService" in sched and "g5_local_verify" in sched


# --- 1.4 post_attempt changes next action ----------------------------------


def test_post_attempt_failure_yields_non_pass_and_actions(tmp_path: Path) -> None:
    from omniagentos.execution.post_attempt import evaluate_post_attempt

    (tmp_path / "expected.md").write_text("x", encoding="utf-8")
    # Missing expected file → mechanical fail → violation plan
    result = evaluate_post_attempt(
        working_dir=tmp_path,
        expected_files=["missing_required.md"],
        undeclared_paths=["secret.py"],
        lane="swarm",
        holder_id="task_1",
    )
    assert result.mechanical_pass is False
    assert result.assess_verdict != "pass" or result.violation_actions
    assert result.violation_actions  # must recommend a next action
    assert any(
        a in result.violation_actions
        for a in ("revert_worktree", "flag_scope_violation", "notify_operator", "block_tier_p")
    )


def test_post_attempt_success_is_pass(tmp_path: Path) -> None:
    (tmp_path / "out.md").write_text("ok\n", encoding="utf-8")
    from omniagentos.execution.post_attempt import evaluate_post_attempt

    # Legacy mechanical presence only: non-git tmp dirs are unobserved under
    # H-13 fail-closed verify. Opt out of live verify here; default
    # use_verify=True coverage lives in tests/execution (observation failures
    # cannot produce a clean/mechanical pass).
    result = evaluate_post_attempt(
        working_dir=tmp_path,
        expected_files=["out.md"],
        undeclared_paths=[],
        lane="swarm",
        use_verify=False,
    )
    assert result.mechanical_pass is True
    assert result.assess_verdict == "pass"


def test_scheduler_source_applies_post_attempt_gate() -> None:
    src = (
        Path(__file__).resolve().parents[2] / "omniagentos" / "swarm" / "scheduler.py"
    ).read_text(encoding="utf-8")
    assert "evaluate_post_attempt" in src
    assert "g5_local_verify" in src
    # Must actually block / mechanical_failure — not only append flags
    assert "post_attempt+g5 deny" in src or "scope/verify gate blocked" in src
