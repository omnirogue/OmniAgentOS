/**
 * Fixture data for local development without a live backend. Enabled with
 * ``NEXT_PUBLIC_USE_PULSE_FIXTURES=true``, mirroring the convention used by
 * collab, swarm, and other feature modules.
 */

import type { CapabilitySnapshot, DeltaResponse, MetricName, SeriesResponse } from "./types";

const USE_FIXTURES = process.env.NEXT_PUBLIC_USE_PULSE_FIXTURES === "true";

function makeSeries(metric: MetricName, points: { date: string; value: number }[]): SeriesResponse {
  return { metric, points };
}

function daysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

const TREND_POINTS = [2, 3, 4, 3, 5, 6, 7, 6, 8, 9, 11, 10, 12, 13, 14];
const VERSION_POINTS = [0, 1, 1, 2, 1, 3, 2, 4, 3, 5, 4, 3, 6, 5, 7];
const IMPROVEMENT_POINTS = [0, 1, 0, 1, 1, 2, 1, 3, 2, 4, 3, 4, 5, 6, 5];
const LOOPS_POINTS = [5, 6, 4, 7, 6, 8, 5, 9, 7, 10, 8, 11, 9, 12, 10];
const ACCEPTANCE_POINTS = [0.5, 0.55, 0.6, 0.58, 0.62, 0.65, 0.63, 0.68, 0.7, 0.72, 0.71, 0.74, 0.76, 0.75, 0.78];
const MEMORY_POINTS = [10, 12, 11, 14, 13, 15, 16, 18, 17, 20, 19, 22, 21, 24, 23];
const RELIABILITY_POINTS = [0.85, 0.87, 0.88, 0.9, 0.89, 0.91, 0.92, 0.93, 0.94, 0.93, 0.95, 0.96, 0.95, 0.97, 0.96];

export const FIXTURE_SERIES: Record<MetricName, SeriesResponse> = {
  "skills.total": makeSeries("skills.total", TREND_POINTS.map((v, i) => ({ date: daysAgo(14 - i), value: v + 40 }))),
  "skills.versions": makeSeries("skills.versions", VERSION_POINTS.map((v, i) => ({ date: daysAgo(14 - i), value: v + 10 }))),
  "improvements.applied": makeSeries("improvements.applied", IMPROVEMENT_POINTS.map((v, i) => ({ date: daysAgo(14 - i), value: v + 20 }))),
  "loops.fires": makeSeries("loops.fires", LOOPS_POINTS.map((v, i) => ({ date: daysAgo(14 - i), value: v + 3 }))),
  "loops.acceptance": makeSeries("loops.acceptance", ACCEPTANCE_POINTS.map((v, i) => ({ date: daysAgo(14 - i), value: v }))),
  "memory.facts": makeSeries("memory.facts", MEMORY_POINTS.map((v, i) => ({ date: daysAgo(14 - i), value: v + 80 }))),
  "reliability.score": makeSeries("reliability.score", RELIABILITY_POINTS.map((v, i) => ({ date: daysAgo(14 - i), value: v }))),
};

export const FIXTURE_DELTA: DeltaResponse = {
  since: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
  skills_updated: 4,
  improvements_decided: 7,
  loops_run: 12,
  tasks_completed: 3,
  chats_active: 2,
};

export const FIXTURE_ACTIVE_ROUTINES = [
  { id: "rtn_nightly_lint", name: "Nightly Lint & Fix", status: "active", lastRunStatus: "passed", nextFire: "in 4h 12m", acceptanceRate: 0.82 },
  { id: "rtn_skill_review", name: "Skill Library Review", status: "active", lastRunStatus: "failed", nextFire: "in 22h", acceptanceRate: 0.67 },
  { id: "rtn_reliability_watch", name: "Reliability Watch", status: "active", lastRunStatus: "passed", nextFire: "in 1h 30m", acceptanceRate: 0.91 },
  { id: "rtn_backlog_executor", name: "Backlog Executor", status: "active", lastRunStatus: "passed", nextFire: "in 6h", acceptanceRate: 0.74 },
];

export const FIXTURE_CAPABILITY: CapabilitySnapshot = {
  topElo: 1384,
  topConfig: "Panel · Claude+Codex+Grok",
  ranked: 9,
  subjects: 3,
  tournamentsCompleted: 12,
};

export { USE_FIXTURES };
