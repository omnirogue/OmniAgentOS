"""Typed graph contracts (Swarm Graph V2)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from omniagentos.contracts import new_id, utc_now_iso

NodeType = Literal["worker", "reduce", "verify", "synthesize", "anchor", "human_gate", "fan_out"]
NodeStatus = Literal[
    "pending", "ready", "running", "completed", "failed", "blocked", "skipped", "cancelled"
]
GraphStatus = Literal[
    "planning", "running", "blocked", "completed", "failed", "cancelled", "partial"
]
CompletenessPolicy = Literal["fail_closed", "allow_partial"]
EdgeKind = Literal["data", "resource", "safety"]


class PortSpec(BaseModel):
    name: str
    artifact_type: str = "json"
    schema_hint: str = ""
    required: bool = True


class NodeSpec(BaseModel):
    key: str
    node_type: NodeType
    title: str
    inputs: list[PortSpec] = Field(default_factory=list)
    outputs: list[PortSpec] = Field(default_factory=list)
    resource_claims: list[str] = Field(default_factory=list)
    verify_policy: dict[str, Any] = Field(default_factory=dict)
    model_role: str | None = None
    effort: str | None = None
    depends_on: list[str] = Field(default_factory=list)  # node keys


class EdgeSpec(BaseModel):
    from_node: str
    to_node: str
    from_port: str = "output"
    to_port: str = "input"
    artifact_type: str | None = None
    required: bool = True
    edge_kind: EdgeKind = "data"


class GraphTemplate(BaseModel):
    slug: str
    version: str = "1.0.0"
    title: str
    description: str = ""
    nodes: list[NodeSpec]
    edges: list[EdgeSpec] = Field(default_factory=list)
    completeness_policy: CompletenessPolicy = "fail_closed"


class GraphArtifact(BaseModel):
    id: str = Field(default_factory=lambda: new_id("gar"))
    graph_run_id: str
    node_key: str
    port: str
    artifact_type: str
    content_hash: str
    content_uri: str
    schema_ok: bool = True
    verified: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)


# Built-in diamond template: fan-out → reduce → verify → synthesize
DIAMOND_TEMPLATE = GraphTemplate(
    slug="diamond-v1",
    title="Fan-out → Reduce → Verify → Synthesize",
    description="Primary reusable graph product for breadth then judgment.",
    nodes=[
        NodeSpec(
            key="fan_a",
            node_type="fan_out",
            title="Worker A (breadth)",
            outputs=[PortSpec(name="finding", artifact_type="finding")],
            model_role="fast_implementer",
            effort="low",
        ),
        NodeSpec(
            key="fan_b",
            node_type="fan_out",
            title="Worker B (breadth)",
            outputs=[PortSpec(name="finding", artifact_type="finding")],
            model_role="fast_implementer",
            effort="low",
        ),
        NodeSpec(
            key="reduce",
            node_type="reduce",
            title="Deterministic reduce",
            inputs=[
                PortSpec(name="finding_a", artifact_type="finding"),
                PortSpec(name="finding_b", artifact_type="finding"),
            ],
            outputs=[PortSpec(name="reduced", artifact_type="reduced_batch")],
            depends_on=["fan_a", "fan_b"],
        ),
        NodeSpec(
            key="verify",
            node_type="verify",
            title="Fresh verifier",
            inputs=[PortSpec(name="candidate", artifact_type="reduced_batch")],
            outputs=[PortSpec(name="verdict", artifact_type="verification_report")],
            verify_policy={"fresh_context": True, "lenses": ["correctness", "support"]},
            model_role="verifier",
            effort="medium",
            depends_on=["reduce"],
        ),
        NodeSpec(
            key="synthesize",
            node_type="synthesize",
            title="Final synthesis",
            inputs=[
                PortSpec(name="reduced", artifact_type="reduced_batch"),
                PortSpec(name="verdict", artifact_type="verification_report"),
            ],
            outputs=[PortSpec(name="deliverable", artifact_type="deliverable")],
            model_role="strong_synthesizer",
            effort="high",
            depends_on=["verify"],
        ),
    ],
    edges=[
        EdgeSpec(from_node="fan_a", to_node="reduce", from_port="finding", to_port="finding_a"),
        EdgeSpec(from_node="fan_b", to_node="reduce", from_port="finding", to_port="finding_b"),
        EdgeSpec(from_node="reduce", to_node="verify", from_port="reduced", to_port="candidate"),
        EdgeSpec(from_node="reduce", to_node="synthesize", from_port="reduced", to_port="reduced"),
        EdgeSpec(from_node="verify", to_node="synthesize", from_port="verdict", to_port="verdict"),
    ],
)

BUILTIN_TEMPLATES = (DIAMOND_TEMPLATE,)


def detect_cycles(nodes: list[NodeSpec], edges: list[EdgeSpec]) -> list[list[str]]:
    """Return list of cycles (node-key paths) if the graph is cyclic."""
    adj: dict[str, list[str]] = {n.key: [] for n in nodes}
    for e in edges:
        adj.setdefault(e.from_node, []).append(e.to_node)
    for n in nodes:
        for dep in n.depends_on:
            adj.setdefault(dep, []).append(n.key)

    cycles: list[list[str]] = []
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in adj}
    stack: list[str] = []

    def dfs(u: str) -> None:
        color[u] = GRAY
        stack.append(u)
        for v in adj.get(u, []):
            if color.get(v, WHITE) == WHITE:
                dfs(v)
            elif color.get(v) == GRAY:
                # cycle: from v to end of stack
                if v in stack:
                    i = stack.index(v)
                    cycles.append(stack[i:] + [v])
        stack.pop()
        color[u] = BLACK

    for k in list(adj.keys()):
        if color.get(k, WHITE) == WHITE:
            dfs(k)
    return cycles


def topological_order(nodes: list[NodeSpec], edges: list[EdgeSpec]) -> list[str]:
    """Kahn topological order; raises ValueError on cycle."""
    cyc = detect_cycles(nodes, edges)
    if cyc:
        raise ValueError(f"graph has cycles: {cyc[0]}")
    indeg: dict[str, int] = {n.key: 0 for n in nodes}
    adj: dict[str, list[str]] = {n.key: [] for n in nodes}
    for e in edges:
        if e.to_node in indeg and e.from_node in indeg:
            adj[e.from_node].append(e.to_node)
            indeg[e.to_node] += 1
    for n in nodes:
        for dep in n.depends_on:
            if dep in indeg and n.key in indeg:
                adj[dep].append(n.key)
                indeg[n.key] += 1
    q = [k for k, d in indeg.items() if d == 0]
    order: list[str] = []
    while q:
        u = q.pop(0)
        order.append(u)
        for v in adj.get(u, []):
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    if len(order) != len(nodes):
        raise ValueError("graph has cycles or disconnected deps")
    return order


def compile_template(template: GraphTemplate) -> dict[str, Any]:
    """GraphCompiler: validate ports, edges, cycles; emit critical path order."""
    node_keys = {n.key for n in template.nodes}
    errors: list[str] = []
    for e in template.edges:
        if e.from_node not in node_keys:
            errors.append(f"edge from unknown node {e.from_node}")
        if e.to_node not in node_keys:
            errors.append(f"edge to unknown node {e.to_node}")
    cycles = detect_cycles(template.nodes, template.edges)
    if cycles:
        errors.append(f"cycle:{cycles[0]}")
    order: list[str] = []
    if not errors:
        try:
            order = topological_order(template.nodes, template.edges)
        except ValueError as exc:
            errors.append(str(exc))
    ready = []
    if not errors:
        deps: dict[str, set[str]] = {n.key: set() for n in template.nodes}
        for e in template.edges:
            deps.setdefault(e.to_node, set()).add(e.from_node)
        for n in template.nodes:
            for d in n.depends_on:
                deps.setdefault(n.key, set()).add(d)
        ready = [k for k, d in deps.items() if not d]
    return {
        "ok": not errors,
        "errors": errors,
        "order": order,
        "ready_keys": ready,
        "slug": template.slug,
        "node_count": len(template.nodes),
        "edge_count": len(template.edges),
        "completeness_policy": template.completeness_policy,
    }
