/**
 * Fixture data for local development without a live backend. Enabled with
 * ``NEXT_PUBLIC_USE_TESTING_FIXTURES=true``, mirroring the pulse feature's
 * convention. Fixtures are NEVER an error fallback — the fixture short-circuit
 * only fires behind this explicit env flag, checked in client.ts before any
 * network call is attempted.
 *
 * Deliberately includes the `available:false` memory case (today's real
 * production state — memcert persists no durable runs yet) so the absent
 * state is exercised in fixture-mode dev the same way it is in prod.
 */

import type {
  TestObsOverview,
  TestObsSeriesResponse,
  WeakspotSources,
  WeakspotsResponse,
} from "./types";

const USE_FIXTURES = process.env.NEXT_PUBLIC_USE_TESTING_FIXTURES === "true";

function daysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d.toISOString().slice(0, 10);
}

function trend(base: number, spread: number, days: number, jitter: number[]): { date: string; value: number }[] {
  return Array.from({ length: days }, (_, i) => ({
    date: daysAgo(days - 1 - i),
    value: Math.round((base + (jitter[i % jitter.length] ?? 0) * spread) * 10) / 10,
  }));
}

const JITTER = [0, 0.4, -0.2, 0.6, 0.1, -0.5, 0.3, 0.8, -0.1, 0.5, 0.2, -0.3, 0.7, 0.4];

export const FIXTURE_OVERVIEW: TestObsOverview = {
  generated_at: new Date().toISOString(),
  northstar: {
    available: true,
    run_id: "nscert-t1-20260813T101005Z",
    verdict_ts: new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString(),
    distance: 23.7,
    gate_pass_rate: 88.2,
    checks_pass: 52,
    checks_fail: 1,
    checks_not_evaluable: 166,
    checks_void: 0,
    delta_distance: 1.4,
  },
  // The real, current state: memcert persists nothing durable today.
  memory: { available: false, reason: "no durable memcert runs recorded yet" },
  diagnostics: {
    available: true,
    features_total: 14,
    features_failing: 2,
    features_stale: 3,
    last_run_ts: new Date(Date.now() - 45 * 60 * 1000).toISOString(),
    aborted_recent: 2,
  },
  suite: {
    available: true,
    runs_7d: 41,
    pass_rate_7d: 93.1,
    latest_lane: {
      name: "fast-lane",
      tests: 11510,
      failures: 3,
      errors: 0,
      skipped: 12,
      ts: new Date(Date.now() - 20 * 60 * 1000).toISOString(),
    },
  },
};

export const FIXTURE_SERIES: Record<string, TestObsSeriesResponse> = {
  northstar: {
    category: "northstar",
    days: 14,
    available: true,
    series: [
      { metric: "nsc.distance", label: "Distance", unit: "%", points: trend(22, 3, 14, JITTER) },
      { metric: "nsc.gate_pass_rate", label: "Gate pass rate", unit: "%", points: trend(87, 4, 14, JITTER) },
      { metric: "nsc.mechanics_refusal_ratio", label: "Mechanics refusal ratio", unit: "%", points: trend(12, 2, 14, JITTER) },
      { metric: "nsc.detection_rate", label: "Detection rate", unit: "%", points: trend(76, 5, 14, JITTER) },
      { metric: "nsc.void_ratio", label: "Void ratio", unit: "%", points: trend(0.5, 0.3, 14, JITTER) },
    ],
  },
  // memory always {available:false} today — no durable runs to build a series from.
  memory: {
    category: "memory",
    days: 14,
    available: false,
    reason: "no durable memcert runs recorded yet",
  },
  diagnostics: {
    category: "diagnostics",
    days: 14,
    available: true,
    series: [
      { metric: "fh.tier1.pass_rate", label: "Tier 1 pass rate", unit: "%", points: trend(94, 3, 14, JITTER) },
      { metric: "fh.tier2.pass_rate", label: "Tier 2 pass rate", unit: "%", points: trend(88, 4, 14, JITTER) },
      { metric: "fh.tier3.pass_rate", label: "Tier 3 pass rate", unit: "%", points: trend(81, 6, 14, JITTER) },
    ],
  },
  suite: {
    category: "suite",
    days: 14,
    available: true,
    series: [
      { metric: "gate.pass_rate", label: "Gate pass rate", unit: "%", points: trend(92, 3, 14, JITTER) },
      { metric: "gate.runs", label: "Runs", unit: "count", points: trend(6, 2, 14, JITTER) },
    ],
  },
};

/** Sources for the main (ranked, non-empty) fixture below — matches the rest
 * of this file's honest theme: memory is genuinely unavailable today. */
const RANKED_SOURCES: WeakspotSources = {
  northstar: { available: true, reason: null },
  memory: { available: false, reason: "no durable memcert runs recorded yet" },
  diagnostics: { available: true, reason: null },
};

export const FIXTURE_WEAKSPOTS: WeakspotsResponse = {
  generated_at: new Date().toISOString(),
  sources: RANKED_SOURCES,
  items: [
    {
      category: "northstar",
      id: "NSC-C43-07",
      label: "NSC-C43-07",
      status: "FAIL",
      detail: "Gate check failed: distance regression exceeded threshold on the mechanics-refusal case.",
      gate: true,
      since: daysAgo(1),
    },
    {
      category: "diagnostics",
      id: "memory/tier2",
      label: "memory · tier2",
      status: "FAIL",
      detail: "3 failing: test_recall_ranking, test_decay_sweep, test_consolidation_dedupe",
      since: daysAgo(2),
    },
    {
      category: "northstar",
      id: "NSC-C21-03",
      label: "NSC-C21-03",
      status: "NOT_EVALUABLE",
      detail: "Judge could not score the case — response truncated before the verdict marker.",
      since: daysAgo(3),
    },
    {
      category: "suite",
      id: "fast-lane",
      label: "fast-lane",
      status: "STALE",
      detail: "No run recorded in the trailing window.",
      since: daysAgo(5),
    },
  ],
};

/**
 * Genuine all-clear (grok T2-M2): zero items AND every source available —
 * the only shape the panel's green "Nothing ranked" state may render for.
 * Not wired into fetchTestObsWeakspots (that always returns the ranked
 * FIXTURE_WEAKSPOTS above for local dev); exported for direct use in tests.
 */
export const FIXTURE_WEAKSPOTS_ALL_CLEAR: WeakspotsResponse = {
  generated_at: new Date().toISOString(),
  items: [],
  sources: {
    northstar: { available: true, reason: null },
    memory: { available: true, reason: null },
    diagnostics: { available: true, reason: null },
  },
};

/**
 * Coverage gap (grok T2-M2): zero items but a source is down — must NEVER
 * render the same green checkmark as the all-clear above.
 */
export const FIXTURE_WEAKSPOTS_SOURCES_DOWN: WeakspotsResponse = {
  generated_at: new Date().toISOString(),
  items: [],
  sources: {
    northstar: { available: true, reason: null },
    memory: { available: false, reason: "no durable memcert runs recorded yet" },
    diagnostics: { available: false, reason: "feature-health ledger unreadable for the current month" },
  },
};

/**
 * Partial coverage WITH a real ranking (grok T2-R2-N3): items are present —
 * the table has real rows to show — but diagnostics is still down. The
 * ready (non-empty) path must surface this the same way the empty path
 * does, not silently drop it just because there was something to rank.
 */
export const FIXTURE_WEAKSPOTS_PARTIAL_COVERAGE: WeakspotsResponse = {
  generated_at: new Date().toISOString(),
  items: [
    {
      category: "northstar",
      id: "NSC-C43-07",
      label: "NSC-C43-07",
      status: "FAIL",
      detail: "Gate check failed: distance regression exceeded threshold on the mechanics-refusal case.",
      gate: true,
      since: daysAgo(1),
    },
  ],
  sources: {
    northstar: { available: true, reason: null },
    memory: { available: true, reason: null },
    diagnostics: { available: false, reason: "feature-health ledger unreadable for the current month" },
  },
};

export { USE_FIXTURES };
