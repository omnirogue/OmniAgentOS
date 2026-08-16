/** Wire types for GET /api/projects/portfolio (Phase B backend). */

export type PortfolioState =
  | "blocked"
  | "failing"
  | "running"
  | "idle"
  | "healthy";

export type PortfolioProject = {
  id: string;
  name: string;
  parent_id: string | null;
  kind: "project" | "scratch" | string;
  path: string[];
  depth: number;
  state: PortfolioState | string;
  rollup_state: PortfolioState | string;
  doing: string;
  blocked_count: number;
  failed_count: number;
  running_count: number;
  budget_usd: number | null;
  spent_usd: number | null;
  last_activity_at: string | null;
};

export type PortfolioResponse = {
  generated_at: string;
  projects: PortfolioProject[];
  scratch_count: number;
  scratch: PortfolioProject[];
};

/** Tree node assembled client-side from the flat portfolio list. */
export type PortfolioTreeNode = {
  project: PortfolioProject;
  children: PortfolioTreeNode[];
};
