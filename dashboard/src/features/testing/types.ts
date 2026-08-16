/**
 * Wire contract for the Observatory /testing (Test Observability) surface —
 * verbatim field names from the testobs design contract (docs/../testobs
 * design v1, 2026-08-14). Read-only: NO writes, absence rendered as absence.
 *
 * Every category is representable as `{ available: false, reason }` — never
 * coerced to zero or a fake success. `memory` is `{available:false}` in
 * production today (memcert persists nothing durable yet); the other three
 * categories are typed the same way for the general case the backend
 * documents ("Absent category → {available:false, reason}").
 */

export type TestObsCategory = "northstar" | "memory" | "diagnostics" | "suite";

export const TESTOBS_CATEGORIES: readonly TestObsCategory[] = [
  "northstar",
  "memory",
  "diagnostics",
  "suite",
] as const;

/**
 * GET /api/testobs/overview — northstar block. Backend sends honest nulls
 * (grok T2-M1): `distance`/`gate_pass_rate`/`delta_distance` are undefined
 * when there is nothing to measure yet, and `checks_fail` is specifically
 * null when the eval_results couldn't be split into pass/fail at all (pure
 * mechanics refusal — a fundamentally different state than "zero failed").
 * `checks_pass`/`checks_not_evaluable`/`checks_void` stay plain counts —
 * they're always well-defined once the run exists, never undecidable on
 * their own.
 */
export type NorthstarOverview =
  | {
      available: true;
      run_id: string;
      verdict_ts: string;
      distance: number | null;
      gate_pass_rate: number | null;
      checks_pass: number;
      checks_fail: number | null;
      checks_not_evaluable: number;
      checks_void: number;
      delta_distance: number | null;
    }
  | { available: false; reason: string };

/**
 * GET /api/testobs/overview — memory block. The wire contract's example only
 * shows the (today-universal) `available:false` case; the `available:true`
 * shape matches the backend reader's memcert envelope
 * (omniagentos/testobs/readers.py): runs/pass_rate/last_run_ts plus the
 * JUnit testsuite counts.
 */
export type MemoryOverview =
  | {
      available: true;
      runs: number;
      /** Null when unmeasured (e.g. zero tests to divide by) — never coerced
       * to 0 (grok T2-M1: backend sends honest nulls). */
      pass_rate: number | null;
      last_run_ts: string;
      tests: number;
      failures: number;
      errors: number;
      skipped: number;
    }
  | { available: false; reason: string };

/** GET /api/testobs/overview — diagnostics block. */
export type DiagnosticsOverview =
  | {
      available: true;
      features_total: number;
      features_failing: number;
      features_stale: number;
      last_run_ts: string;
      aborted_recent: number;
    }
  | { available: false; reason: string };

export type LatestLaneSummary = {
  name: string;
  tests: number;
  failures: number;
  errors: number;
  skipped: number;
  ts: string;
};

/**
 * GET /api/testobs/overview — suite block.
 * `pass_rate_7d` is null when there were zero runs in the window to compute
 * a rate from (grok T2-M1). `runs_7d` is independently nullable (backend
 * contract update): null means "no merge-gate receipt store exists at all"
 * — the count itself is unmeasured, not zero — while a real `0` means the
 * store exists and has history, it just saw zero runs in the window. These
 * are different facts and must render differently: null → "—", `0` → "0".
 */
export type SuiteOverview =
  | {
      available: true;
      runs_7d: number | null;
      pass_rate_7d: number | null;
      latest_lane: LatestLaneSummary | null;
    }
  | { available: false; reason: string };

export type TestObsOverview = {
  generated_at: string;
  northstar: NorthstarOverview;
  memory: MemoryOverview;
  diagnostics: DiagnosticsOverview;
  suite: SuiteOverview;
};

export type TestObsPoint = { date: string; value: number };

export type TestObsSeriesLine = {
  metric: string;
  label: string;
  unit: string;
  points: TestObsPoint[];
};

/** GET /api/testobs/series?category=&days= */
export type TestObsSeriesResponse =
  | { category: TestObsCategory; days: number; available: true; series: TestObsSeriesLine[] }
  | { category: TestObsCategory; days: number; available: false; reason: string };

/**
 * Every status a weak-spot item can carry — a CLOSED eight-value union
 * (backend contract), emitted worst-first by the server:
 *
 *   FAIL          — an actual failing check/test.
 *   ERR           — errored while measuring (an exception, not a graded
 *                    failure — still not success).
 *   ABORT         — the run never happened at all (a load guard shed it).
 *   MISS          — declared paths are absent; the measurement is
 *                    incomplete, not merely empty.
 *   EMPTY         — the run happened but measured nothing.
 *   STALE         — the run stopped running (no run recorded in the
 *                    trailing window).
 *   VOID          — ruled void/inapplicable (e.g. northstar checks_void).
 *   NOT_EVALUABLE — mechanics refusal — the judge/harness couldn't score it.
 *
 * NONE of these is success; none may ever render green/ok-toned — see
 * weakspotTone() in tiles.ts, which maps every one of the eight explicitly
 * and falls back to neutral (never green) for anything outside this set.
 * The type itself is intentionally closed (no `(string & {})` escape hatch)
 * now that the full set is pinned by the backend contract; weakspotTone()
 * still accepts a plain `string` at the call site as a runtime safety net,
 * since a TS type gives no guarantee about what actually arrives over the
 * wire.
 */
export type WeakspotStatus = "FAIL" | "ERR" | "ABORT" | "MISS" | "EMPTY" | "STALE" | "VOID" | "NOT_EVALUABLE";

export type WeakspotItem = {
  category: TestObsCategory;
  id: string;
  label: string;
  status: WeakspotStatus;
  detail: string;
  /** Present on northstar gate-check rows only. */
  gate?: boolean;
  since: string;
};

/** The three categories the weakspots ranking draws from (suite does not
 * feed the ranking — its own runs/failures already surface on its stat tile
 * and trend panel). */
export type WeakspotSourceCategory = "northstar" | "memory" | "diagnostics";

/** The EXPECTED source keys, as a value — iterate this, never
 * `Object.keys(sources)` (grok T2-R2-N1). Object.keys would let an empty or
 * partial `sources` object read as an all-clear (favourable absence) and
 * would let an unexpected extra key (e.g. a future `suite`) silently count
 * as a ranking source the backend never claimed it was. */
export const WEAKSPOT_SOURCE_CATEGORIES: readonly WeakspotSourceCategory[] = [
  "northstar",
  "memory",
  "diagnostics",
] as const;

export type WeakspotSourceInfo = { available: boolean; reason: string | null };

/**
 * Per-source availability for the weakspots ranking (grok T2-M2). An empty
 * `items` list is only a genuine "nothing ranked" all-clear when every
 * source here is `available:true` — if a source is down, the ranking is
 * incomplete and an empty list is a coverage gap, not a clean bill of
 * health. Never render the all-clear state without checking this.
 */
export type WeakspotSources = Record<WeakspotSourceCategory, WeakspotSourceInfo>;

/** GET /api/testobs/weakspots */
export type WeakspotsResponse = {
  generated_at: string;
  items: WeakspotItem[];
  sources: WeakspotSources;
};
