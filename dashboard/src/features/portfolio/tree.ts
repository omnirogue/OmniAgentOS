/** Client-side tree assembly + expansion persistence for the portfolio rail. */

import type { PortfolioProject, PortfolioTreeNode } from "./types";

const EXPANDED_KEY = "oaos.portfolio.tree.expanded";
const SCRATCH_KEY = "oaos.portfolio.scratch.expanded";

/** Build a forest from the flat durable project list (parent_id edges). */
export function buildForest(projects: PortfolioProject[]): PortfolioTreeNode[] {
  const byId = new Map<string, PortfolioTreeNode>();
  for (const project of projects) {
    byId.set(project.id, { project, children: [] });
  }

  const roots: PortfolioTreeNode[] = [];
  for (const node of byId.values()) {
    const parentId = node.project.parent_id;
    if (parentId && byId.has(parentId)) {
      byId.get(parentId)!.children.push(node);
    } else {
      roots.push(node);
    }
  }

  const sortRecursive = (nodes: PortfolioTreeNode[]) => {
    nodes.sort((a, b) => a.project.name.localeCompare(b.project.name));
    for (const n of nodes) sortRecursive(n.children);
  };
  sortRecursive(roots);
  return roots;
}

/** Direct-child count for the rail counter. */
export function childCount(node: PortfolioTreeNode): number {
  return node.children.length;
}

export function loadExpandedIds(projectIds: string[]): Set<string> {
  if (typeof window === "undefined") return defaultExpanded(projectIds, []);
  try {
    const raw = window.localStorage.getItem(EXPANDED_KEY);
    if (!raw) return defaultExpanded(projectIds, []);
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return defaultExpanded(projectIds, []);
    const known = new Set(projectIds);
    return new Set(parsed.filter((id): id is string => typeof id === "string" && known.has(id)));
  } catch {
    return defaultExpanded(projectIds, []);
  }
}

/** Default: expand roots (and any node with depth 0). */
export function defaultExpanded(
  projectIds: string[],
  projects: Array<{ id: string; depth: number }>,
): Set<string> {
  if (projects.length === 0) {
    // Without depth info, expand nothing until data arrives.
    return new Set();
  }
  return new Set(projects.filter((p) => p.depth === 0).map((p) => p.id));
}

export function saveExpandedIds(ids: Set<string>): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(EXPANDED_KEY, JSON.stringify([...ids]));
  } catch {
    /* quota / private mode */
  }
}

/** Scratch group is collapsed by default; only true when user has expanded it. */
export function loadScratchExpanded(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(SCRATCH_KEY) === "1";
  } catch {
    return false;
  }
}

export function saveScratchExpanded(open: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(SCRATCH_KEY, open ? "1" : "0");
  } catch {
    /* ignore */
  }
}

/** Visible nodes given expansion state (depth-first preorder). */
export function flattenVisible(
  forest: PortfolioTreeNode[],
  expanded: Set<string>,
): PortfolioTreeNode[] {
  const out: PortfolioTreeNode[] = [];
  const walk = (nodes: PortfolioTreeNode[]) => {
    for (const node of nodes) {
      out.push(node);
      if (node.children.length > 0 && expanded.has(node.project.id)) {
        walk(node.children);
      }
    }
  };
  walk(forest);
  return out;
}

/** Simple substring fuzzy match over name + path for ⌘K `#` scope. */
export function fuzzyMatchProject(
  project: PortfolioProject,
  query: string,
): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const hay = `${project.name} ${(project.path ?? []).join(" ")}`.toLowerCase();
  // All tokens must appear (order-independent) — light fuzzy without a dep.
  return q.split(/\s+/).every((token) => hay.includes(token));
}
