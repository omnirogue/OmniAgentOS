/**
 * Pure data shaping for the /testing surface — no React, no fetch, easy to
 * unit test (see tiles.test.ts alongside). Two jobs:
 *   1. Turn a TestObsOverview + the matching series into the 4 stat-row
 *      tiles (TestObsStatRow.tsx).
 *   2. Turn TestObsSeriesLine[] into @/design LineChart props, and shape the
 *      weakspots sources/coverage state for the table (WeakSpotsPanel.tsx).
 *      Item ORDER is the backend's alone — the client never re-ranks.
 *
 * UNKNOWN is never coerced to 0 or success-styled (dashboard-conventions.md
 * Rule 2) — every shape function below renders an unavailable category as
 * "—" with the backend's reason, never a fabricated number.
 */

import type { LineSeries } from "@/design";
import type {
  TestObsCategory,
  TestObsOverview,
  TestObsSeriesLine,
  TestObsSeriesResponse,
  WeakspotItem,
  WeakspotSourceCategory,
  WeakspotSourceInfo,
  WeakspotSources,
  WeakspotStatus,
} from "./types";
import { WEAKSPOT_SOURCE_CATEGORIES } from "./types";

export type StatTone = "default" | "ok" | "warn" | "danger" | "accent";

export type TestObsStatItem = {
  category: TestObsCategory;
  title: string;
  primary: string;
  caption: string;
  sparkPoints: number[];
  tone: StatTone;
  available: boolean;
};

const CATEGORY_TITLES: Record<TestObsCategory, string> = {
  northstar: "North Star",
  memory: "Memory",
  diagnostics: "Diagnostics",
  suite: "Suite",
};

/** The one series line each stat tile sparklines on. */
const PRIMARY_METRIC: Record<TestObsCategory, string> = {
  northstar: "nsc.distance",
  memory: "memcert.pass_rate",
  diagnostics: "fh.tier1.pass_rate",
  suite: "gate.pass_rate",
};

/** Find one named metric line in a series list; null when absent. */
export function findSeriesLine(lines: TestObsSeriesLine[] | undefined, metric: string): TestObsSeriesLine | null {
  if (!lines) return null;
  return lines.find((l) => l.metric === metric) ?? null;
}

/** Plain values off a line, oldest-first — [] for a null/empty line. */
export function seriesValues(line: TestObsSeriesLine | null): number[] {
  return (line?.points ?? []).map((p) => p.value);
}

/** Latest point's value, or null on an empty/absent line (never 0). */
export function latestPoint(line: TestObsSeriesLine | null): number | null {
  const pts = line?.points ?? [];
  if (pts.length === 0) return null;
  return pts[pts.length - 1]!.value;
}

/** Oldest→newest delta, or null when there are fewer than 2 points. */
export function deltaFromPoints(line: TestObsSeriesLine | null): number | null {
  const pts = line?.points ?? [];
  if (pts.length < 2) return null;
  return pts[pts.length - 1]!.value - pts[0]!.value;
}

/** Renders a percent, or "—" on null — the backend sends honest nulls for
 * rates it cannot compute (grok T2-M1); never coerce a missing rate to 0. */
function fmtPct(v: number | null, digits = 1): string {
  if (v == null) return "—";
  return `${v.toFixed(digits)}%`;
}

/** Tone for an unavailable category's stat tile — always neutral, never a
 * coerced success or failure color (Rule 2: UNKNOWN is never success-styled). */
const UNAVAILABLE_TONE: StatTone = "default";

/**
 * North Star tile tone (grok T2-N1). `checks_fail` null means the run
 * couldn't be split into pass/fail at all — that is unknown, not "0 fail",
 * so it renders neutral rather than a coerced green. When `checks_fail` is
 * a real 0 but mechanics refusals (`checks_not_evaluable`) outnumber actual
 * passes, the sample is dominated by refusals, not passing tests — that is
 * a warning, not a clean bill of health.
 */
export function northstarTone(
  checksFail: number | null,
  checksPass: number,
  checksNotEvaluable: number,
): StatTone {
  if (checksFail == null) return "default";
  if (checksFail > 0) return "danger";
  if (checksNotEvaluable > checksPass) return "warn";
  return "ok";
}

/**
 * Shape one category's stat tile from the overview + its series response.
 * `lines` is the series list already fetched for this category (empty array
 * is fine — the sparkline just renders empty). Unavailable categories (most
 * notably memory, `{available:false}` in production today) render a neutral
 * "—" headline with the backend's reason as the caption — never a coerced
 * zero, never success-styled (Rule 2).
 */
export function shapeStat(
  category: TestObsCategory,
  overview: TestObsOverview,
  lines: TestObsSeriesLine[] = [],
): TestObsStatItem {
  const title = CATEGORY_TITLES[category];
  const line = findSeriesLine(lines, PRIMARY_METRIC[category]);
  const sparkPoints = seriesValues(line);

  if (category === "northstar") {
    const ns = overview.northstar;
    if (!ns.available) {
      return { category, title, primary: "—", caption: ns.reason, sparkPoints: [], tone: UNAVAILABLE_TONE, available: false };
    }
    return {
      category,
      title,
      primary: fmtPct(ns.distance),
      caption: `${fmtPct(ns.gate_pass_rate)} gate pass · ${ns.checks_pass} pass / ${ns.checks_fail ?? "—"} fail`,
      sparkPoints,
      tone: northstarTone(ns.checks_fail, ns.checks_pass, ns.checks_not_evaluable),
      available: true,
    };
  }

  if (category === "memory") {
    const mem = overview.memory;
    if (!mem.available) {
      return { category, title, primary: "—", caption: mem.reason, sparkPoints: [], tone: UNAVAILABLE_TONE, available: false };
    }
    return {
      category,
      title,
      primary: fmtPct(mem.pass_rate),
      caption: `${mem.tests} tests · ${mem.failures} failing`,
      sparkPoints,
      // grok T2-N2: errors are not failures but are just as much "not clean"
      // — a suite that errored out is not a passing suite. grok T2-R2-N2:
      // a null pass_rate means the rate itself is unmeasured (e.g. zero
      // tests) — danger/ok only make sense once there IS a measured rate;
      // otherwise this must stay neutral like every other unknown.
      tone: mem.pass_rate == null ? UNAVAILABLE_TONE : mem.failures + mem.errors > 0 ? "danger" : "ok",
      available: true,
    };
  }

  if (category === "diagnostics") {
    const diag = overview.diagnostics;
    if (!diag.available) {
      return { category, title, primary: "—", caption: diag.reason, sparkPoints: [], tone: UNAVAILABLE_TONE, available: false };
    }
    return {
      category,
      title,
      primary: `${diag.features_failing} failing`,
      caption: `${diag.features_total} features · ${diag.features_stale} stale · ${diag.aborted_recent} aborted`,
      sparkPoints,
      tone: diag.features_failing > 0 ? "danger" : diag.features_stale > 0 ? "warn" : "ok",
      available: true,
    };
  }

  // suite
  const suite = overview.suite;
  if (!suite.available) {
    return { category, title, primary: "—", caption: suite.reason, sparkPoints: [], tone: UNAVAILABLE_TONE, available: false };
  }
  const lane = suite.latest_lane;
  // grok (backend contract): null means no merge-gate receipt store exists
  // at all (unmeasured) — never coerced to 0. A real 0 (store exists, zero
  // runs in the window) still renders as "0", not "—".
  const runsText = suite.runs_7d == null ? "—" : `${suite.runs_7d}`;
  return {
    category,
    title,
    primary: fmtPct(suite.pass_rate_7d),
    caption: lane
      ? `${runsText} runs (7d) · ${lane.name}: ${lane.failures} failing / ${lane.tests} tests`
      : `${runsText} runs (7d)`,
    sparkPoints,
    tone:
      suite.pass_rate_7d == null || suite.runs_7d == null
        ? UNAVAILABLE_TONE
        : suite.pass_rate_7d >= 95
          ? "ok"
          : suite.pass_rate_7d >= 80
            ? "warn"
            : "danger",
    available: true,
  };
}

/** Whether a /series response should render as "empty" (no chart to draw) —
 * either the backend marked the category unavailable, or every line came
 * back with zero points. */
export function seriesIsEmpty(res: TestObsSeriesResponse | null): boolean {
  if (!res) return true;
  if (!res.available) return true;
  return res.series.every((l) => l.points.length === 0);
}

/** Adapts testobs series lines into @/design LineChart's `series` prop. */
export function toLineSeries(lines: TestObsSeriesLine[]): LineSeries[] {
  return lines.map((line) => ({
    name: line.label || line.metric,
    points: line.points.map((p) => ({ x: p.date, y: p.value })),
  }));
}

/**
 * Tone for a weak-spot status badge. Maps every one of the eight closed
 * WeakspotStatus values explicitly (backend contract) — no fall-through
 * default could ever paint an unrecognized value green: the function's own
 * return type has no "ok" branch, so the worst that can happen to a truly
 * unknown status is a neutral badge, never a coerced success.
 *
 *   FAIL, ERR                 → danger (an actual failure or error).
 *   ABORT, MISS, EMPTY, STALE → warn (mechanics/coverage gaps, not a graded
 *                                failure — same tone the panel already used
 *                                for STALE, now applied consistently to its
 *                                three siblings).
 *   NOT_EVALUABLE             → warn (unchanged — mechanics refusal, kept
 *                                grouped with STALE as before this union
 *                                closed).
 *   VOID                      → neutral (unchanged — was never handled
 *                                explicitly before and fell through to the
 *                                neutral default; kept that way explicitly).
 */
export function weakspotTone(status: string): "danger" | "warn" | "neutral" {
  switch (status as WeakspotStatus) {
    case "FAIL":
    case "ERR":
      return "danger";
    case "ABORT":
    case "MISS":
    case "EMPTY":
    case "STALE":
    case "NOT_EVALUABLE":
      return "warn";
    case "VOID":
      return "neutral";
    default:
      return "neutral";
  }
}

/** A missing source key reads as unavailable, never as an all-clear
 * (grok T2-R2-N1) — a backend that dropped a key told us nothing, and
 * "nothing" is not "available:true". */
const MISSING_SOURCE: WeakspotSourceInfo = { available: false, reason: "not reported" };

function sourceInfo(sources: WeakspotSources | null | undefined, key: WeakspotSourceCategory): WeakspotSourceInfo {
  return sources?.[key] ?? MISSING_SOURCE;
}

/**
 * Whether every EXPECTED weakspots source reports available:true (grok
 * T2-M2, hardened T2-R2-N1). Only in this case is an empty `items` list a
 * genuine "nothing ranked" all-clear — the backend owns the ranking and its
 * completeness; the client no longer re-sorts (grok T2-N3), it only judges
 * whether the ranking is trustworthy. Iterates the three EXPECTED keys from
 * the typed contract (WEAKSPOT_SOURCE_CATEGORIES) rather than
 * `Object.keys(sources)`: an empty/partial `sources` object must NOT
 * all-clear (favourable absence), and an unexpected extra key (e.g. a
 * future `suite`) must never be treated as a ranking source.
 */
export function allSourcesAvailable(sources: WeakspotSources | null | undefined): boolean {
  return WEAKSPOT_SOURCE_CATEGORIES.every((key) => sourceInfo(sources, key).available);
}

/** The subset of the EXPECTED weakspots sources that are unavailable, with
 * their reasons — powers the "coverage incomplete" state instead of a false
 * all-clear. A key absent from `sources` entirely reports as unavailable
 * with reason "not reported" (grok T2-R2-N1); an unexpected extra key is
 * ignored, same as in allSourcesAvailable. */
export function unavailableSources(
  sources: WeakspotSources | null | undefined,
): Array<{ category: WeakspotSourceCategory; reason: string | null }> {
  return WEAKSPOT_SOURCE_CATEGORIES.filter((key) => !sourceInfo(sources, key).available).map((key) => ({
    category: key,
    reason: sourceInfo(sources, key).reason,
  }));
}

/** Groups weak-spot items by category — powers an optional "N across M
 * categories" summary line without re-deriving it in the component. */
export function groupWeakspotsByCategory(items: WeakspotItem[]): Record<string, WeakspotItem[]> {
  const groups: Record<string, WeakspotItem[]> = {};
  for (const item of items) {
    (groups[item.category] ??= []).push(item);
  }
  return groups;
}
