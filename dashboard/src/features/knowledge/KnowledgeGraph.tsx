"use client";

import { useMemo, useState } from "react";
import { Card, EmptyState } from "../../design";
import type { KnowledgeGraphNode, KnowledgeGraphResponse } from "./types";

const WIDTH = 1000;
const HEIGHT = 560;
const STATUS_COLOR = { active: "var(--promote)", quarantined: "var(--warn)", superseded: "var(--text-faint)" } as const;

type PositionedNode = KnowledgeGraphNode & { x: number; y: number };
function layout(nodes: KnowledgeGraphNode[]): PositionedNode[] {
  const facts = nodes.filter((node): node is Extract<KnowledgeGraphNode, { kind: "fact" }> => node.kind === "fact");
  const entities = nodes.filter((node): node is Extract<KnowledgeGraphNode, { kind: "entity" }> => node.kind === "entity");
  const place = <T extends KnowledgeGraphNode>(items: T[], radius: number, offset: number) => items.map((node, index) => {
    const theta = offset + (Math.PI * 2 * index) / Math.max(items.length, 1);
    return { ...node, x: WIDTH / 2 + Math.cos(theta) * radius, y: HEIGHT / 2 + Math.sin(theta) * radius };
  });
  return [...place(facts, 190, -Math.PI / 2), ...place(entities, 94, Math.PI / 5)];
}
function shortLabel(label: string): string { return label.length > 28 ? `${label.slice(0, 27)}…` : label; }

export function KnowledgeGraph({ graph }: { graph: KnowledgeGraphResponse }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const nodes = useMemo(() => layout(graph.nodes), [graph.nodes]);
  const byId = useMemo(() => new Map<string, PositionedNode>(nodes.map((node) => [node.id, node])), [nodes]);
  if (!nodes.length) return <EmptyState title="No knowledge graph yet" message="Facts and entities will appear here as they are recorded." />;
  const selected = selectedId ? byId.get(selectedId) : null;
  return (
    <Card padding="sm">
      <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-3)", margin: "var(--space-1) 0 var(--space-3)", color: "var(--text-muted)", fontSize: "var(--text-small)" }}>
        <span>● Fact</span><span style={{ color: "var(--accent)" }}>◆ Entity</span><span style={{ color: "var(--promote)" }}>● Active</span><span style={{ color: "var(--warn)" }}>● Quarantined</span><span style={{ color: "var(--text-faint)" }}>● Superseded</span>
      </div>
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={`${nodes.length} knowledge nodes connected by ${graph.edges.length} synapses`} style={{ display: "block", width: "100%", height: "auto", minHeight: 340, background: "var(--canvas)", border: "1px solid var(--border)", borderRadius: "var(--radius-md)" }}>
        {graph.edges.map((edge) => { const source = byId.get(edge.source); const target = byId.get(edge.target); if (!source || !target) return null; const connected = selectedId === edge.source || selectedId === edge.target; return <line key={edge.id} x1={source.x} y1={source.y} x2={target.x} y2={target.y} stroke={connected ? "var(--accent)" : "var(--border-strong)"} strokeWidth={1 + edge.weight * 3} opacity={selectedId && !connected ? 0.15 : 0.68}><title>{edge.type} · weight {edge.weight.toFixed(2)}</title></line>; })}
        {nodes.map((node) => { const isFact = node.kind === "fact"; const isSelected = node.id === selectedId; const fill = isFact ? STATUS_COLOR[node.status] : "var(--accent)"; const dim = selectedId && !isSelected && !graph.edges.some((edge) => edge.source === selectedId && edge.target === node.id || edge.target === selectedId && edge.source === node.id); return <g key={node.id} role="button" tabIndex={0} aria-label={node.label} onClick={() => setSelectedId(node.id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setSelectedId(node.id); } }} style={{ cursor: "pointer", opacity: dim ? 0.28 : 1 }}><circle cx={node.x} cy={node.y} r={isFact ? 11 + (node.importance * 6) : 9} fill={fill} stroke={isSelected ? "var(--text)" : "var(--canvas)"} strokeWidth={isSelected ? 3 : 2} /><text x={node.x} y={node.y + 27} textAnchor="middle" fill="var(--text-muted)" fontSize="13">{shortLabel(node.label)}</text><title>{node.label}</title></g>; })}
      </svg>
      {selected ? <p style={{ margin: "var(--space-3) 0 0", color: "var(--text-muted)", fontSize: "var(--text-small)" }}><strong style={{ color: "var(--text)" }}>{selected.label}</strong>{selected.kind === "fact" ? ` · ${selected.discipline} · ${Math.round(selected.trust * 100)}% trust` : ` · ${selected.entityKind}`}</p> : null}
    </Card>
  );
}
