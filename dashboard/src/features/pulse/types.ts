/** Pinned contracts for the Observatory /pulse surface (FINAL-PLAN.md §B). */

export type MetricName =
  | "skills.total"
  | "skills.versions"
  | "improvements.applied"
  | "loops.fires"
  | "loops.acceptance"
  | "memory.facts"
  | "reliability.score";

export const METRIC_NAMES: readonly MetricName[] = [
  "skills.total",
  "skills.versions",
  "improvements.applied",
  "loops.fires",
  "loops.acceptance",
  "memory.facts",
  "reliability.score",
] as const;

export type PulsePoint = { date: string; value: number };

export type SeriesResponse = {
  metric: MetricName | string;
  points: PulsePoint[];
};

export type MetricsResponse = { metrics: string[] };

/** Pinned GET /api/system/delta contract. */
export type DeltaResponse = {
  since: string;
  skills_updated: number;
  improvements_decided: number;
  loops_run: number;
  tasks_completed: number;
  chats_active: number;
};

/** Snapshot of what one Observatory tile renders — decouples data from UI. */
export type PulseTileData = {
  title: string;
  primary: string | number;
  caption?: string;
  delta?: { value: string | number; trend: "up" | "down" | "neutral" };
  sparkPoints: number[];
  /** Where the tile deep-links (always the full page, e.g. "/skills"). */
  deepLink: string;
  /** Semantic tone for the headline number. */
  tone?: "default" | "ok" | "warn" | "danger" | "accent" | "promote" | "reject";
};

/**
 * Capability tile snapshot (FINAL-PLAN §6 tile 4: "ELO delta, tournaments won
 * (leaderboard data)"). Derived live from /api/lab/leaderboard +
 * /api/lab/tournaments — the leaderboard FEATURE's labApi client is pinned to
 * its own dev fixtures, so the Observatory reads the endpoints directly.
 */
export type CapabilitySnapshot = {
  /** Highest ELO on the board (null when no configs are ranked yet). */
  topElo: number | null;
  /** Label/config id of the top-rated config, when known. */
  topConfig: string | null;
  /** Ranked configs across all subjects. */
  ranked: number;
  /** Distinct subjects with at least one ranked config. */
  subjects: number;
  /** Tournaments that reached a terminal state (status "done"). */
  tournamentsCompleted: number;
};
