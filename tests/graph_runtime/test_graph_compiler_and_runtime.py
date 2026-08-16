"""Graph V2 compiler + runtime: cycles, ordering, fail-closed, isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.graph_runtime.contracts import (
    DIAMOND_TEMPLATE,
    EdgeSpec,
    GraphTemplate,
    NodeSpec,
    PortSpec,
    compile_template,
    detect_cycles,
    topological_order,
)
from omniagentos.graph_runtime.service import GraphRuntimeService


@pytest.fixture()
def svc(tmp_path: Path) -> GraphRuntimeService:
    return GraphRuntimeService(db_path=str(tmp_path / "g.db"))


def test_compile_diamond_ok(svc: GraphRuntimeService) -> None:
    report = svc.compile("diamond-v1")
    assert report["ok"] is True
    assert report["errors"] == []
    assert report["order"][0] in {"fan_a", "fan_b"}
    assert report["order"][-1] == "synthesize"
    assert set(report["ready_keys"]) == {"fan_a", "fan_b"}


def test_cycle_detection_blocks_start() -> None:
    cyclic = GraphTemplate(
        slug="cyclic",
        title="bad",
        nodes=[
            NodeSpec(key="a", node_type="worker", title="A", outputs=[PortSpec(name="o")]),
            NodeSpec(key="b", node_type="worker", title="B", outputs=[PortSpec(name="o")]),
        ],
        edges=[
            EdgeSpec(from_node="a", to_node="b", from_port="o", to_port="i"),
            EdgeSpec(from_node="b", to_node="a", from_port="o", to_port="i"),
        ],
    )
    cycles = detect_cycles(cyclic.nodes, cyclic.edges)
    assert cycles
    report = compile_template(cyclic)
    assert report["ok"] is False
    assert any("cycle" in e for e in report["errors"])


def test_topological_order_diamond() -> None:
    order = topological_order(DIAMOND_TEMPLATE.nodes, DIAMOND_TEMPLATE.edges)
    assert order.index("fan_a") < order.index("reduce")
    assert order.index("fan_b") < order.index("reduce")
    assert order.index("reduce") < order.index("verify")
    assert order.index("verify") < order.index("synthesize")


def test_parallel_ready_nodes(svc: GraphRuntimeService) -> None:
    run = svc.start_diamond(title="ready")
    ready = svc.ready_nodes(run["id"])
    keys = {n["key"] for n in ready}
    assert keys == {"fan_a", "fan_b"}


def test_failed_dependency_propagation(svc: GraphRuntimeService) -> None:
    run = svc.start_diamond(title="fail-prop", completeness_policy="fail_closed")
    rid = run["id"]
    svc.complete_node(rid, "fan_a", outputs={"finding": {"claim": "a", "score": 0.9}})
    svc.complete_node(rid, "fan_b", outputs={}, error="worker crashed")
    again = svc.get_run(rid)
    assert again is not None
    assert again["status"] in {"failed", "blocked"}
    reduce = next(n for n in again["nodes"] if n["key"] == "reduce")
    assert reduce["status"] in {"blocked", "pending", "failed"}


def test_unverified_blocks_synthesis(svc: GraphRuntimeService) -> None:
    run = svc.start_diamond(title="unver")
    rid = run["id"]
    svc.complete_node(rid, "fan_a", outputs={"finding": {"claim": "a", "score": 0.8}})
    svc.complete_node(rid, "fan_b", outputs={"finding": {"claim": "b", "score": 0.8}})
    svc.complete_node(rid, "reduce", outputs={"reduced": {"items": [], "count": 0}})
    svc.complete_node(rid, "verify", outputs={"verdict": {"passed": True}}, verified=False)
    synth = next(n for n in svc.get_run(rid)["nodes"] if n["key"] == "synthesize")
    assert synth["status"] == "blocked"


def test_independent_verification_required_flag(svc: GraphRuntimeService) -> None:
    """Verify node outputs only count as verified when caller sets verified=True."""
    run = svc.run_diamond_deterministic(title="v-ok")
    assert run["status"] == "completed"
    arts = run["artifacts"]
    verify_arts = [a for a in arts if a["node_key"] == "verify"]
    assert verify_arts
    assert all(int(a.get("verified") or 0) == 1 for a in verify_arts)


def test_multi_project_graph_isolation(svc: GraphRuntimeService) -> None:
    r1 = svc.start_diamond(title="p1", company_id="co-a", project_id="proj-a")
    r2 = svc.start_diamond(title="p2", company_id="co-b", project_id="proj-b")
    assert r1["id"] != r2["id"]
    assert r1.get("company_id") == "co-a"
    assert r2.get("project_id") == "proj-b"
    g1 = svc.get_run(r1["id"])
    g2 = svc.get_run(r2["id"])
    assert g1 is not None and g2 is not None
    assert {n["id"] for n in g1["nodes"]}.isdisjoint({n["id"] for n in g2["nodes"]})


def test_missing_required_output_rejected(svc: GraphRuntimeService) -> None:
    run = svc.start_diamond(title="schema")
    with pytest.raises(ValueError, match="missing required"):
        svc.complete_node(run["id"], "fan_a", outputs={})


def test_artifact_hash_and_edge_binding(svc: GraphRuntimeService) -> None:
    run = svc.run_diamond_deterministic(title="arts")
    view = svc.live_view(run["id"])
    assert all(e["status"] == "bound" for e in view["edge_flow"])
    assert view["artifact_count"] >= 5
    for a in run["artifacts"]:
        assert a["content_hash"]
        assert len(a["content_hash"]) == 64


def test_allow_partial_policy(svc: GraphRuntimeService) -> None:
    run = svc.start_diamond(title="partial", completeness_policy="allow_partial")
    rid = run["id"]
    svc.complete_node(rid, "fan_a", outputs={"finding": {"claim": "only", "score": 0.9}})
    svc.complete_node(rid, "fan_b", outputs={}, error="gone")
    again = svc.get_run(rid)
    assert again is not None
    # Under allow_partial, reduce may become ready with partial bindings or blocked
    reduce = next(n for n in again["nodes"] if n["key"] == "reduce")
    assert reduce["status"] in {"ready", "blocked", "pending"}


def test_live_view_critical_path(svc: GraphRuntimeService) -> None:
    run = svc.run_diamond_deterministic(title="cp")
    view = svc.live_view(run["id"])
    assert view["critical_path"]
    assert view["status"] == "completed"
