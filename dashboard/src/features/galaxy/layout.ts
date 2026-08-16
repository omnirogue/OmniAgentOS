/**
 * Deterministic radial-cluster + force-relax graph layout.
 *
 * NO Math.random anywhere (module scope or otherwise) — the same notes+links always
 * produce the same node positions. Nodes are grouped into type clusters placed evenly
 * around a circle (stable order from noteType.ts); within a cluster, nodes are packed
 * with a phyllotaxis (sunflower) spiral — a classic *deterministic* even-spacing
 * technique keyed only by index/count, not randomness. A short, damped force-relax pass
 * (spring on edges, mutual repulsion, weak pull back to the cluster/global center) then
 * untangles overlaps while keeping clusters coherent. Iteration count is fixed, so this
 * is pure math on a fixed input — reproducible and stable across renders.
 */

import { resolveWikilink, type VaultLinkIndex } from "./linkResolver";
import { noteTypeOrder } from "./noteType";
import type { AnyNoteType, VaultTreeNote } from "./types";

export interface GraphNode extends VaultTreeNote {
  x: number;
  y: number;
  /** Number of edges (either direction) touching this node. */
  degree: number;
}

export interface GraphEdge {
  source: string; // path
  target: string; // path
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** path -> set of directly-connected paths, for O(1) hover-neighbor lookups. */
  adjacency: Map<string, Set<string>>;
  typeCounts: Map<AnyNoteType, number>;
}

export interface LayoutOptions {
  width?: number;
  height?: number;
}

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5)); // deterministic constant (~2.39996 rad)

function sunflowerOffset(index: number, count: number, maxR: number): { dx: number; dy: number } {
  const r = maxR * Math.sqrt((index + 0.5) / Math.max(count, 1));
  const theta = index * GOLDEN_ANGLE;
  return { dx: r * Math.cos(theta), dy: r * Math.sin(theta) };
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

function buildEdges(notes: readonly VaultTreeNote[], index: VaultLinkIndex): GraphEdge[] {
  const seen = new Set<string>();
  const edges: GraphEdge[] = [];
  for (const note of notes) {
    for (const rawLink of note.links) {
      const resolved = resolveWikilink(rawLink, index);
      if (!resolved || resolved.path === note.path) continue;
      const key = [note.path, resolved.path].sort().join(" ");
      if (seen.has(key)) continue;
      seen.add(key);
      edges.push({ source: note.path, target: resolved.path });
    }
  }
  return edges;
}

/** Build graph edges + adjacency from notes and a prebuilt link index (no layout). */
export function buildGraphData(notes: readonly VaultTreeNote[], index: VaultLinkIndex): Omit<GraphData, "nodes"> & { edges: GraphEdge[] } {
  const edges = buildEdges(notes, index);
  const adjacency = new Map<string, Set<string>>();
  const bump = (a: string, b: string) => {
    if (!adjacency.has(a)) adjacency.set(a, new Set());
    adjacency.get(a)!.add(b);
  };
  for (const e of edges) {
    bump(e.source, e.target);
    bump(e.target, e.source);
  }
  const typeCounts = new Map<AnyNoteType, number>();
  for (const n of notes) typeCounts.set(n.type, (typeCounts.get(n.type) ?? 0) + 1);
  return { edges, adjacency, typeCounts };
}

/**
 * Compute stable (x, y) positions for every note. Pure function of `notes` + `edges` —
 * callers should memoize on those identities so this only runs once per data load, not
 * per hover/render (see GalaxyGraph.tsx).
 */
export function computeLayout(
  notes: readonly VaultTreeNote[],
  edges: readonly GraphEdge[],
  opts: LayoutOptions = {},
): Map<string, { x: number; y: number }> {
  const width = opts.width ?? 1000;
  const height = opts.height ?? 700;
  const cx = width / 2;
  const cy = height / 2;
  const margin = 44;

  const groups = new Map<AnyNoteType, VaultTreeNote[]>();
  for (const note of notes) {
    const list = groups.get(note.type) ?? [];
    list.push(note);
    groups.set(note.type, list);
  }
  // Stable within-cluster order (id) so re-fetches of the same data don't jitter.
  for (const list of groups.values()) list.sort((a, b) => a.id.localeCompare(b.id));

  const activeTypes = noteTypeOrder().filter((t) => (groups.get(t)?.length ?? 0) > 0);
  const clusterRadius = activeTypes.length <= 1 ? 0 : Math.min(width, height) * 0.34;

  const posX = new Map<string, number>();
  const posY = new Map<string, number>();
  const clusterCx = new Map<string, number>();
  const clusterCy = new Map<string, number>();

  activeTypes.forEach((type, ti) => {
    const members = groups.get(type)!;
    const angle = (2 * Math.PI * ti) / Math.max(activeTypes.length, 1);
    const cxi = cx + clusterRadius * Math.cos(angle);
    const cyi = cy + clusterRadius * Math.sin(angle);
    const localR = 22 + 13 * Math.sqrt(members.length);
    members.forEach((note, ni) => {
      const { dx, dy } = sunflowerOffset(ni, members.length, localR);
      posX.set(note.path, cxi + dx);
      posY.set(note.path, cyi + dy);
      clusterCx.set(note.path, cxi);
      clusterCy.set(note.path, cyi);
    });
  });

  // Short, damped force relaxation — deterministic (fixed iteration count, no RNG).
  // Skipped above a size threshold to keep very large vaults smooth; the cluster+
  // sunflower placement above is already overlap-light and perfectly stable on its own.
  const n = notes.length;
  if (n > 1 && n <= 320) {
    const paths = notes.map((note) => note.path);
    const idx = new Map(paths.map((p, i) => [p, i]));
    const edgePairs = edges
      .map((e) => [idx.get(e.source), idx.get(e.target)] as const)
      .filter((pair): pair is [number, number] => pair[0] !== undefined && pair[1] !== undefined);

    const x = paths.map((p) => posX.get(p) ?? cx);
    const y = paths.map((p) => posY.get(p) ?? cy);
    const ccx = paths.map((p) => clusterCx.get(p) ?? cx);
    const ccy = paths.map((p) => clusterCy.get(p) ?? cy);

    const REPULSION = 2200;
    const EDGE_LENGTH = 86;
    const SPRING = 0.02;
    const CLUSTER_PULL = 0.018;
    const GLOBAL_PULL = 0.002;
    const STEP = 0.55;
    const MAX_FORCE = 14;
    const ITERATIONS = 46;

    for (let it = 0; it < ITERATIONS; it++) {
      const fx = new Array<number>(n).fill(0);
      const fy = new Array<number>(n).fill(0);

      for (let i = 0; i < n; i++) {
        for (let j = i + 1; j < n; j++) {
          let dx = x[i]! - x[j]!;
          let dy = y[i]! - y[j]!;
          let d2 = dx * dx + dy * dy;
          if (d2 < 4) {
            // Deterministic nudge for coincident points — offsets by index, not RNG.
            dx = 0.5 + ((i - j) % 5);
            dy = 0.5 + ((i + j) % 5);
            d2 = dx * dx + dy * dy;
          }
          const d = Math.sqrt(d2);
          const rep = REPULSION / d2;
          const ux = dx / d;
          const uy = dy / d;
          fx[i]! += ux * rep;
          fy[i]! += uy * rep;
          fx[j]! -= ux * rep;
          fy[j]! -= uy * rep;
        }
      }

      for (const [a, b] of edgePairs) {
        const dx = x[a]! - x[b]!;
        const dy = y[a]! - y[b]!;
        const d = Math.max(Math.sqrt(dx * dx + dy * dy), 0.01);
        const f = (d - EDGE_LENGTH) * SPRING;
        const ux = dx / d;
        const uy = dy / d;
        fx[a]! -= ux * f;
        fy[a]! -= uy * f;
        fx[b]! += ux * f;
        fy[b]! += uy * f;
      }

      for (let i = 0; i < n; i++) {
        fx[i]! += (ccx[i]! - x[i]!) * CLUSTER_PULL;
        fy[i]! += (ccy[i]! - y[i]!) * CLUSTER_PULL;
        fx[i]! += (cx - x[i]!) * GLOBAL_PULL;
        fy[i]! += (cy - y[i]!) * GLOBAL_PULL;
      }

      for (let i = 0; i < n; i++) {
        const cfx = clamp(fx[i]!, -MAX_FORCE, MAX_FORCE);
        const cfy = clamp(fy[i]!, -MAX_FORCE, MAX_FORCE);
        x[i] = clamp(x[i]! + cfx * STEP, margin, width - margin);
        y[i] = clamp(y[i]! + cfy * STEP, margin, height - margin);
      }
    }

    for (let i = 0; i < n; i++) {
      posX.set(paths[i]!, x[i]!);
      posY.set(paths[i]!, y[i]!);
    }
  } else {
    for (const [p, v] of posX) posX.set(p, clamp(v, margin, width - margin));
    for (const [p, v] of posY) posY.set(p, clamp(v, margin, height - margin));
  }

  const out = new Map<string, { x: number; y: number }>();
  for (const note of notes) {
    out.set(note.path, { x: posX.get(note.path) ?? cx, y: posY.get(note.path) ?? cy });
  }
  return out;
}

/** One-shot: build edges/adjacency AND positions from raw notes + a link index. */
export function buildGraph(notes: readonly VaultTreeNote[], index: VaultLinkIndex, opts?: LayoutOptions): GraphData {
  const { edges, adjacency, typeCounts } = buildGraphData(notes, index);
  const positions = computeLayout(notes, edges, opts);
  const degree = new Map<string, number>();
  for (const [path, set] of adjacency) degree.set(path, set.size);
  const nodes: GraphNode[] = notes.map((note) => {
    const pos = positions.get(note.path) ?? { x: (opts?.width ?? 1000) / 2, y: (opts?.height ?? 700) / 2 };
    return { ...note, x: pos.x, y: pos.y, degree: degree.get(note.path) ?? 0 };
  });
  return { nodes, edges, adjacency, typeCounts };
}
