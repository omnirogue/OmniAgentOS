"""Swarm Graph V2 — diamond, completeness, live view, fail-closed fan-in."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.graph_runtime.contracts import (
    DIAMOND_TEMPLATE,
    EdgeSpec,
    GraphTemplate,
    NodeSpec,
    PortSpec,
)
from omniagentos.graph_runtime.service import GraphRuntimeService, NodeCompletionConflict
from omniagentos.graph_runtime.store import GraphStore


@pytest.fixture()
def svc(tmp_path: Path) -> GraphRuntimeService:
    return GraphRuntimeService(db_path=str(tmp_path / "graph.db"))


def test_health_live(svc: GraphRuntimeService) -> None:
    h = svc.health()
    assert h["ok"] is True
    assert h["live"] is True
    assert h["version"] == "graph-v2"
    assert "diamond-v1" in h["templates"]


def test_templates_seeded(svc: GraphRuntimeService) -> None:
    templates = svc.list_templates()
    slugs = {t["slug"] for t in templates}
    assert "diamond-v1" in slugs


def test_diamond_deterministic_completes(svc: GraphRuntimeService) -> None:
    run = svc.run_diamond_deterministic(
        title="unit-diamond",
        findings_a={"claim": "A", "score": 0.85, "source": "a"},
        findings_b={"claim": "B", "score": 0.95, "source": "b"},
    )
    assert run["status"] == "completed"
    statuses = {n["key"]: n["status"] for n in run["nodes"]}
    assert statuses == {
        "fan_a": "completed",
        "fan_b": "completed",
        "reduce": "completed",
        "verify": "completed",
        "synthesize": "completed",
    }
    assert len(run["artifacts"]) >= 5
    # Top finding should be B (higher score)
    synth = next(n for n in run["nodes"] if n["key"] == "synthesize")
    art_id = (synth.get("output_artifacts") or {}).get("deliverable")
    assert art_id
    art = next(a for a in run["artifacts"] if a["id"] == art_id)
    import json

    payload = (
        json.loads(art["payload_json"])
        if isinstance(art["payload_json"], str)
        else art["payload_json"]
    )
    assert payload["top"]["claim"] == "B"


def test_fail_closed_blocks_incomplete_fanin(svc: GraphRuntimeService) -> None:
    run = svc.start_diamond(title="partial", completeness_policy="fail_closed")
    run_id = run["id"]
    # Only complete fan_a — fan_b missing
    svc.complete_node(
        run_id,
        "fan_a",
        outputs={"finding": {"claim": "only-a", "score": 0.9}},
    )
    # Force fan_b failed so reduce cannot wait forever
    svc.complete_node(run_id, "fan_b", outputs={}, error="worker died")
    run2 = svc.get_run(run_id)
    assert run2 is not None
    reduce = next(n for n in run2["nodes"] if n["key"] == "reduce")
    # Upstream failure + fail_closed → reduce blocked or run failed
    assert run2["status"] in {"failed", "blocked"}
    assert reduce["status"] in {"blocked", "pending", "failed"}


def test_missing_required_output_raises(svc: GraphRuntimeService) -> None:
    run = svc.start_diamond(title="schema")
    with pytest.raises(ValueError, match="missing required output"):
        svc.complete_node(run["id"], "fan_a", outputs={})


def test_live_view_edge_flow(svc: GraphRuntimeService) -> None:
    run = svc.run_diamond_deterministic(title="view")
    view = svc.live_view(run["id"])
    assert view["status"] == "completed"
    assert view["artifact_count"] >= 5
    assert any(e["status"] == "bound" for e in view["edge_flow"])
    assert view["critical_path"]


def test_verify_must_be_verified_for_synthesize(tmp_path: Path) -> None:
    """fail_closed: synthesize waits for verified verdict."""
    store = GraphStore(str(tmp_path / "v.db"))
    svc = GraphRuntimeService(store=store)
    run = svc.start_diamond(title="unverified")
    rid = run["id"]
    svc.complete_node(rid, "fan_a", outputs={"finding": {"claim": "a", "score": 0.8}})
    svc.complete_node(rid, "fan_b", outputs={"finding": {"claim": "b", "score": 0.8}})
    svc.complete_node(
        rid,
        "reduce",
        outputs={"reduced": {"items": [], "count": 0}},
    )
    # Complete verify WITHOUT verified=True
    svc.complete_node(
        rid,
        "verify",
        outputs={"verdict": {"passed": True}},
        verified=False,
    )
    run2 = svc.get_run(rid)
    assert run2 is not None
    synth = next(n for n in run2["nodes"] if n["key"] == "synthesize")
    # Should be blocked because verdict not verified under fail_closed
    assert synth["status"] == "blocked"


# --- M-39: idempotent complete_node + fail-closed unbypassable gates ---------


def test_complete_node_idempotent(svc: GraphRuntimeService) -> None:
    """M-39: Repeated completion returns same run; artifacts not duplicated."""
    run = svc.start_diamond(title="idempotent")
    rid = run["id"]
    r1 = svc.complete_node(rid, "fan_a", outputs={"finding": {"claim": "a", "score": 0.8}})
    r2 = svc.complete_node(rid, "fan_a", outputs={"finding": {"claim": "a", "score": 0.9}})
    n1 = next(n for n in r1["nodes"] if n["key"] == "fan_a")
    n2 = next(n for n in r2["nodes"] if n["key"] == "fan_a")
    assert n1["status"] == "completed"
    assert n2["status"] == "completed"
    # Ensure artifacts aren't duplicated (node output artifacts ID should be same)
    assert n1.get("output_artifacts", {}) == n2.get("output_artifacts", {})
    assert len(r1["artifacts"]) == len(r2["artifacts"])


def test_fail_closed_unbypassable(svc: GraphRuntimeService) -> None:
    """M-39: Blocked nodes cannot be force-completed; fail-closed is absolute."""
    run = svc.start_diamond(title="bypass", completeness_policy="fail_closed")
    rid = run["id"]
    # Attempting to synthesize without verified verify node
    svc.complete_node(rid, "fan_a", outputs={"finding": {"claim": "a", "score": 0.8}})
    svc.complete_node(rid, "fan_b", outputs={"finding": {"claim": "b", "score": 0.8}})
    svc.complete_node(rid, "reduce", outputs={"reduced": {"items": [], "count": 0}})
    svc.complete_node(rid, "verify", outputs={"verdict": {"passed": True}}, verified=False)

    r2 = svc.get_run(rid)
    synth = next(n for n in r2["nodes"] if n["key"] == "synthesize")
    assert synth["status"] == "blocked"
    assert "unverified" in synth.get("error", "")
    # Force-complete must be rejected — fail-closed is unbypassable (M-39)
    with pytest.raises(ValueError, match="cannot complete node synthesize"):
        svc.complete_node(rid, "synthesize", outputs={"deliverable": {"top": {}}})
    r3 = svc.get_run(rid)
    synth3 = next(n for n in r3["nodes"] if n["key"] == "synthesize")
    assert synth3["status"] == "blocked"
    assert r3["status"] in {"blocked", "running", "failed"}


# --- FH-001: completion is an atomic claim, not a check-then-write -----------


def test_transition_node_guard_refuses_a_stale_status(svc: GraphRuntimeService) -> None:
    """The UPDATE guard, not a caller's earlier read, decides who owns the node."""
    rid = svc.start_diamond(title="cas")["id"]
    assert svc.store.transition_node(rid, "fan_a", to_status="completed") is True
    # A second claimant still holding the (now stale) ``ready`` read is refused
    # and writes nothing.
    assert svc.store.transition_node(rid, "fan_a", to_status="failed", error="late") is False
    node = next(n for n in svc.store.get_run(rid)["nodes"] if n["key"] == "fan_a")
    assert node["status"] == "completed"
    assert node["error"] is None


def test_lost_claim_publishes_no_artifact(
    svc: GraphRuntimeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completion that loses the claim mid-call is rejected, not merged in."""
    rid = svc.start_diamond(title="conflict")["id"]
    claim = svc.store.transition_node

    def stolen(run_id: str, node_key: str, **kwargs: Any) -> bool:
        # Stand in for the winning writer: it lands between this caller's status
        # read and its own claim, so the real guard is what refuses the claim.
        claim(run_id, node_key, to_status="completed")
        return claim(run_id, node_key, **kwargs)

    monkeypatch.setattr(svc.store, "transition_node", stolen)
    with pytest.raises(NodeCompletionConflict):
        svc.complete_node(rid, "fan_a", outputs={"finding": {"claim": "late", "score": 0.1}})
    monkeypatch.setattr(svc.store, "transition_node", claim)

    after = svc.get_run(rid)
    assert [a for a in after["artifacts"] if a["node_key"] == "fan_a"] == []
    fan_a = next(n for n in after["nodes"] if n["key"] == "fan_a")
    assert fan_a["status"] in {"ready", "running"}
    assert fan_a["output_artifacts"] == {}
    # A ValueError subclass: the /complete route's existing mapping still covers it.
    assert isinstance(NodeCompletionConflict("x"), ValueError)


# --- Redteam addendum (a): the verified-edge gate keys on TYPE, not on name ---
#
# ``_propagate``'s fail-closed completeness check used to ask whether the SOURCE
# node was *called* ``verify``. A verifier that is renamed (or a second,
# nested one) therefore fed ``synthesize`` an unverified artifact with the gate
# silently sitting out. The key is the source node's ``node_type``.


def _template_with(
    slug: str,
    *,
    rename_verify_to: str | None = None,
    verify_node_type: str | None = None,
    extra_verifier: tuple[str, str] | None = None,
) -> GraphTemplate:
    """The built-in diamond, re-keyed/re-typed for one gate-bypass shape.

    ``rename_verify_to`` renames the verify node (its ``node_type`` is kept);
    ``verify_node_type`` retypes the node still KEYED ``verify``;
    ``extra_verifier`` adds ``(key, to_port)`` as a second verify-typed node
    that also feeds ``synthesize``.
    """
    template = DIAMOND_TEMPLATE.model_copy(deep=True)
    template.slug = slug
    if verify_node_type is not None:
        for node in template.nodes:
            if node.key == "verify":
                node.node_type = verify_node_type  # type: ignore[assignment]
    if rename_verify_to is not None:
        for node in template.nodes:
            if node.key == "verify":
                node.key = rename_verify_to
            node.depends_on = [
                rename_verify_to if dep == "verify" else dep for dep in node.depends_on
            ]
        for edge in template.edges:
            if edge.from_node == "verify":
                edge.from_node = rename_verify_to
            if edge.to_node == "verify":
                edge.to_node = rename_verify_to
    if extra_verifier is not None:
        key, to_port = extra_verifier
        template.nodes.append(
            NodeSpec(
                key=key,
                node_type="verify",
                title="Second verifier",
                inputs=[PortSpec(name="candidate", artifact_type="reduced_batch")],
                outputs=[PortSpec(name="verdict", artifact_type="verification_report")],
                depends_on=["reduce"],
            )
        )
        for node in template.nodes:
            if node.key == "synthesize":
                node.inputs.append(PortSpec(name=to_port, artifact_type="verification_report"))
        template.edges.append(
            EdgeSpec(from_node="reduce", to_node=key, from_port="reduced", to_port="candidate")
        )
        template.edges.append(
            EdgeSpec(from_node=key, to_node="synthesize", from_port="verdict", to_port=to_port)
        )
    return template


def _run_to_the_verify_edge(svc: GraphRuntimeService, template: GraphTemplate) -> str:
    """Start ``template`` and complete everything upstream of the verifiers."""
    run_id = str(svc.store.create_run(title=template.slug, template=template)["id"])
    svc.complete_node(run_id, "fan_a", outputs={"finding": {"claim": "a", "score": 0.8}})
    svc.complete_node(run_id, "fan_b", outputs={"finding": {"claim": "b", "score": 0.9}})
    svc.complete_node(run_id, "reduce", outputs={"reduced": {"items": [], "count": 0}})
    return run_id


def _node(svc: GraphRuntimeService, run_id: str, key: str) -> dict[str, Any]:
    run = svc.get_run(run_id)
    assert run is not None
    return next(n for n in run["nodes"] if n["key"] == key)


def test_renamed_verify_node_still_gates_synthesize(svc: GraphRuntimeService) -> None:
    """RED before the fix: keyed on the NAME, ``qa_gate`` bypassed the gate and
    synthesize went ``ready`` on an unverified verification report."""
    run_id = _run_to_the_verify_edge(
        svc, _template_with("diamond-renamed-verify", rename_verify_to="qa_gate")
    )

    svc.complete_node(run_id, "qa_gate", outputs={"verdict": {"passed": True}}, verified=False)

    synth = _node(svc, run_id, "synthesize")
    assert synth["status"] == "blocked"
    assert "qa_gate.verdict:unverified" in (synth.get("error") or "")
    # And the gate is unbypassable, exactly as it is for the canonical name.
    with pytest.raises(ValueError, match="cannot complete node synthesize"):
        svc.complete_node(run_id, "synthesize", outputs={"deliverable": {"top": {}}})


def test_renamed_verify_node_releases_synthesize_once_verified(
    svc: GraphRuntimeService,
) -> None:
    """The type-keyed gate must not OVER-block: a verified renamed verifier
    still hands synthesize its inputs."""
    run_id = _run_to_the_verify_edge(
        svc, _template_with("diamond-renamed-verified", rename_verify_to="qa_gate")
    )

    svc.complete_node(run_id, "qa_gate", outputs={"verdict": {"passed": True}}, verified=True)

    synth = _node(svc, run_id, "synthesize")
    assert synth["status"] == "ready"
    assert set(synth["input_bindings"]) == {"reduced", "verdict"}


def test_every_verify_typed_edge_is_gated_not_just_the_canonical_one(
    svc: GraphRuntimeService,
) -> None:
    """Adversarial ORDERING: the canonical ``verify`` lands VERIFIED first, so a
    check that stops at the first verify-typed edge (or only looks at the one
    named ``verify``) sees a clean fan-in. The nested second verifier's
    unverified verdict must still block."""
    run_id = _run_to_the_verify_edge(
        svc, _template_with("diamond-two-verifiers", extra_verifier=("qa_gate", "verdict_b"))
    )

    svc.complete_node(run_id, "verify", outputs={"verdict": {"passed": True}}, verified=True)
    assert _node(svc, run_id, "synthesize")["status"] == "pending"
    svc.complete_node(run_id, "qa_gate", outputs={"verdict": {"passed": True}}, verified=False)

    synth = _node(svc, run_id, "synthesize")
    assert synth["status"] == "blocked"
    assert "qa_gate.verdict:unverified" in (synth.get("error") or "")


def test_a_node_merely_named_verify_is_not_treated_as_a_verifier(
    svc: GraphRuntimeService,
) -> None:
    """The mutation catcher for re-adding the name clause: a ``worker`` that
    happens to be KEYED ``verify`` is not a verifier, so its artifact is not
    held to ``verified`` — the gate follows the type, in both directions."""
    run_id = _run_to_the_verify_edge(
        svc, _template_with("diamond-worker-named-verify", verify_node_type="worker")
    )

    svc.complete_node(run_id, "verify", outputs={"verdict": {"passed": True}}, verified=False)

    synth = _node(svc, run_id, "synthesize")
    assert synth["status"] == "ready", synth.get("error")
    assert set(synth["input_bindings"]) == {"reduced", "verdict"}
