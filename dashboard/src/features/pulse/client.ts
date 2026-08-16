/**
 * Observatory pulse fetchers — thin wrappers over `api.get` matching the
 * pinned contracts in FINAL-PLAN.md §B. Fixture fallbacks (local dev with
 * ``NEXT_PUBLIC_USE_PULSE_FIXTURES=true``) live in ./fixtures.ts and are
 * returned by the hooks when the live API is unavailable.
 */

import { api } from "@/lib/api";
import { formatCountdown } from "@/features/routines/countdown";
import type { RecentRunItem, Routine } from "@/features/routines/types";
import type {
  CapabilitySnapshot,
  DeltaResponse,
  MetricsResponse,
  MetricName,
  SeriesResponse,
} from "./types";
import {
  FIXTURE_ACTIVE_ROUTINES,
  FIXTURE_CAPABILITY,
  FIXTURE_DELTA,
  FIXTURE_SERIES,
  USE_FIXTURES,
} from "./fixtures";

export function fetchPulseSeries(metric: MetricName, days = 30): Promise<SeriesResponse> {
  if (USE_FIXTURES) {
    const points = FIXTURE_SERIES[metric].points.slice(-days);
    return Promise.resolve({ metric, points });
  }
  return api.get<SeriesResponse>(
    `/api/pulse/series?metric=${encodeURIComponent(metric)}&days=${days}`,
  );
}

export function fetchPulseMetrics(): Promise<MetricsResponse> {
  if (USE_FIXTURES) {
    return Promise.resolve({ metrics: Object.keys(FIXTURE_SERIES) });
  }
  return api.get<MetricsResponse>("/api/pulse/metrics");
}

export function fetchSystemDelta(since: string): Promise<DeltaResponse> {
  if (USE_FIXTURES) return Promise.resolve(FIXTURE_DELTA);
  return api.get<DeltaResponse>(
    `/api/system/delta?since=${encodeURIComponent(since)}`,
  );
}

export type ActiveRoutineRow = {
  id: string;
  name: string;
  status: string;
  lastRunStatus: string;
  nextFire: string;
  /**
   * `null` means UNKNOWN — the routine has produced no judgeable result yet
   * (every run so far parked awaiting a human, or had nothing to do). It is
   * deliberately not coerced to 0: rendering "0%" for a loop that is behaving
   * correctly is the same lie the backend refuses to persist.
   */
  acceptanceRate: number | null;
};

/** Outcome label for one routine's newest settled run (aggregate is newest-first). */
function lastRunStatusFor(routineId: string, runs: RecentRunItem[]): string {
  const run = runs.find((r) => r.routine_id === routineId);
  if (!run) return "pending";
  if (run.accepted === true) return "accepted";
  if (run.accepted === false) return "rejected";
  if (run.gate_passed === true) return "passed";
  if (run.gate_passed === false) return "failed";
  return "pending";
}

/**
 * Active routines with last-run context for LoopsPulse.tsx. Joins the live
 * endpoints: ``GET /api/routines?status=active`` (roster + trigger config) ×
 * the S5 ``GET /api/routines/runs`` aggregate (newest run per routine). The
 * next-fire countdown is computed from the cron expression by the same
 * formatCountdown the Loops page uses — never from a guessed field.
 * Errors propagate: the tile renders its ErrorState instead of stale fixtures.
 */
export async function fetchActiveRoutines(limit = 5): Promise<ActiveRoutineRow[]> {
  if (USE_FIXTURES) return FIXTURE_ACTIVE_ROUTINES.slice(0, limit);
  const [routines, runsBody] = await Promise.all([
    api.get<Routine[]>("/api/routines?status=active"),
    api.get<{ runs: RecentRunItem[] }>("/api/routines/runs?limit=50"),
  ]);
  const runs = Array.isArray(runsBody?.runs) ? runsBody.runs : [];
  return (Array.isArray(routines) ? routines : []).slice(0, limit).map((r) => ({
    id: r.id,
    name: r.name,
    status: r.status,
    lastRunStatus: lastRunStatusFor(r.id, runs),
    nextFire: formatCountdown(r.trigger_type, r.trigger_config),
    acceptanceRate: r.acceptance_rate ?? null,
  }));
}

/**
 * Capability tile snapshot (FINAL-PLAN §6: "ELO delta, tournaments won
 * (leaderboard data)"). Reads /api/lab/leaderboard + /api/lab/tournaments
 * directly. Empty leaderboard → topElo null, the tile renders its empty state.
 */
export async function fetchCapabilitySnapshot(): Promise<CapabilitySnapshot> {
  if (USE_FIXTURES) return FIXTURE_CAPABILITY;
  const [leaderboard, tournaments] = await Promise.all([
    api.get<Array<Record<string, unknown>>>("/api/lab/leaderboard"),
    api.get<Array<Record<string, unknown>>>("/api/lab/tournaments"),
  ]);
  const rows = Array.isArray(leaderboard) ? leaderboard : [];
  let topElo: number | null = null;
  let topConfig: string | null = null;
  const subjects = new Set<string>();
  for (const row of rows) {
    const elo = Number(row.elo ?? row.rating ?? NaN);
    if (!Number.isFinite(elo)) continue;
    if (typeof row.subject === "string" && row.subject) subjects.add(row.subject);
    if (topElo === null || elo > topElo) {
      topElo = elo;
      topConfig =
        (typeof row.label === "string" && row.label) ||
        (typeof row.config_id === "string" && row.config_id) ||
        null;
    }
  }
  const completed = (Array.isArray(tournaments) ? tournaments : []).filter(
    (t) => t.status === "done",
  ).length;
  return {
    topElo,
    topConfig,
    ranked: rows.length,
    subjects: subjects.size,
    tournamentsCompleted: completed,
  };
}
