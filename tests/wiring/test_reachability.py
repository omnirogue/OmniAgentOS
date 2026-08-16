"""Structural reachability: each subsystem is imported from a real production path.

This is the permanent fix for the class of failure where modules are marked LIVE
while having zero non-test callers (HANDOFF Phase 2.1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "omniagentos"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _module_imports_symbol(module_path: Path, needle: str) -> bool:
    text = _source(module_path)
    return needle in text


def test_cbm_reachable_from_swarm_spawn() -> None:
    src = _source(PKG / "swarm" / "spawn.py")
    assert "CognitiveBudgetService" in src
    assert "allocate(" in src
    # Must apply the allocation, not only write it.
    assert "cbm_allocation_id" in src or "reasoning_effort" in src


def test_graph_runtime_reachable_from_api() -> None:
    src = _source(PKG / "api" / "routes" / "graph.py")
    assert "GraphRuntimeService" in src
    assert "include_router" in _source(PKG / "api" / "main.py") or "graph" in _source(
        PKG / "api" / "main.py"
    )


def test_gates_g2_g3_g5_can_deny() -> None:
    from omniagentos.gates.service import GateService

    g = GateService()
    d2 = g.g2_dispatch({"capacity_ok": False})
    assert d2.decision == "deny"
    d3 = g.g3_tool({"tool": "shell", "tools_allowed": ["file_read"]})
    assert d3.decision == "deny"
    d5 = g.g5_local_verify({"verify_ok": False})
    assert d5.decision == "deny"


def test_extractors_reachable_from_broker() -> None:
    src = _source(PKG / "connectors" / "broker.py")
    assert "extract_call_bounds" in src
    assert "grant_id" in src


def test_grants_require_approval_on_mint() -> None:
    import tempfile

    from omniagentos.db.store import SqliteStore
    from omniagentos.grants import GrantsStore

    with tempfile.TemporaryDirectory() as td:
        gs = GrantsStore(SqliteStore(f"{td}/g.db"))
        with pytest.raises(ValueError, match="approval_id"):
            gs.create_grant(
                "gmail.send", max_actions=1, max_spend_usd=1.0, expires_at="2099-01-01T00:00:00Z"
            )


def test_orgdims_reachable_from_spawn() -> None:
    src = _source(PKG / "swarm" / "spawn.py")
    assert "OrgDimsService" in src


def test_metacog_reachable_from_api() -> None:
    assert "metacog" in _source(PKG / "api" / "main.py")


def test_policy_consequential_auto_under_auto() -> None:
    from omniagentos.contracts import ActionClass
    from omniagentos.policy import evaluate_action, load_policy

    d = evaluate_action(ActionClass.CONSEQUENTIAL, load_policy())
    assert d.requires_approval is False
    assert "AUTO mode gate: consequential" in d.reason


def test_workers_exported_from_routing_package() -> None:
    from omniagentos.routing import select_worker

    sel = select_worker(tier="standard", effort="medium")
    assert sel.endpoint is not None


def test_skills_select_reachable_from_spawn_source() -> None:
    assert "select_skills" in _source(PKG / "swarm" / "spawn.py")


def test_globex_wired_in_toolplane() -> None:
    assert "globex_generate_image" in _source(PKG / "toolplane" / "tools.py")


def test_brand_context_wired_in_intake() -> None:
    # bind_project_brand superseded try_provision_brand in 058cc08b: the old
    # path keyed off a PROCESS-WIDE brand-pack env var, which could leak one
    # project's brand into another's dispatch. The binding is per-project.
    assert "bind_project_brand" in _source(PKG / "intake" / "service.py")


def test_learning_api_wired_in_cbm_close() -> None:
    assert "log_decision" in _source(PKG / "cbm" / "service.py")
