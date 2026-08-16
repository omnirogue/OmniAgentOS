/**
 * Tile data shaping — maps live (or fixture) series responses into the
 * {@link PulseTileData} shape PulseTile.tsx consumes. Pure functions,
 * easy to test in isolation (see vitest alongside).
 */

import type { CapabilitySnapshot, PulseTileData, SeriesResponse } from "./types";

/**
 * Latest value from a series, or 0 when empty.
 */
export function latestValue(series: SeriesResponse | null): number {
  if (!series || series.points.length === 0) return 0;
  return series.points[series.points.length - 1]!.value;
}

/**
 * Absolute change between the oldest and newest value in the window.
 * Positive means "grew" — caller decides how to render.
 */
export function deltaFromSeries(series: SeriesResponse | null): number {
  if (!series || series.points.length < 2) return 0;
  const first = series.points[0]!.value;
  const last = series.points[series.points.length - 1]!.value;
  return last - first;
}

/**
 * Format a number for display in a tile headline. Keeps 2 decimals for
 * rates (acceptance, reliability), 0 for absolute counts.
 */
export function formatValue(metric: string, value: number): string {
  if (metric === "loops.acceptance" || metric === "reliability.score") {
    // Render as percentage with one decimal.
    return `${(value * 100).toFixed(1)}%`;
  }
  if (Number.isInteger(value)) return String(value);
  return value.toFixed(1);
}

/**
 * Direction of the trend — for the delta badge on a tile.
 */
export function trendFromDelta(delta: number): "up" | "down" | "neutral" {
  if (delta > 0) return "up";
  if (delta < 0) return "down";
  return "neutral";
}

/**
 * Shape the Skills tile from live series data.
 */
export function shapeSkillsTile(total: SeriesResponse | null, versions: SeriesResponse | null): PulseTileData {
  const totalVal = latestValue(total);
  const versVal = latestValue(versions);
  return {
    title: "Skills",
    primary: formatValue("skills.total", totalVal),
    caption: `${versVal} versions this week`,
    delta: {
      value: deltaFromSeries(total),
      trend: trendFromDelta(deltaFromSeries(total)),
    },
    sparkPoints: (total?.points ?? []).map((p) => p.value),
    deepLink: "/skills",
    tone: "accent",
  };
}

/**
 * Shape the Self-improvement tile. Shows applied-in-production count
 * with a sparkline of applied-per-day.
 */
export function shapeImprovementsTile(series: SeriesResponse | null): PulseTileData {
  const val = latestValue(series);
  return {
    title: "Self-improvement",
    primary: formatValue("improvements.applied", val),
    caption: "applied changes in production",
    delta: {
      value: deltaFromSeries(series),
      trend: trendFromDelta(deltaFromSeries(series)),
    },
    sparkPoints: (series?.points ?? []).map((p) => p.value),
    deepLink: "/improvements",
    tone: "promote",
  };
}

/**
 * Shape the Loops tile. Shows fires-this-week count and acceptance rate.
 */
export function shapeLoopsTile(fires: SeriesResponse | null, acceptance: SeriesResponse | null): PulseTileData {
  const firesVal = latestValue(fires);
  const accVal = latestValue(acceptance);
  return {
    title: "Loops",
    primary: formatValue("loops.fires", firesVal),
    caption: `fires · ${formatValue("loops.acceptance", accVal)} acceptance`,
    delta: {
      value: deltaFromSeries(fires),
      trend: trendFromDelta(deltaFromSeries(fires)),
    },
    sparkPoints: (fires?.points ?? []).map((p) => p.value),
    deepLink: "/loops",
    tone: "accent",
  };
}

/**
 * Shape the Capability tile (FINAL-PLAN §6 tile 4): ELO leader + ranked-count
 * caption from the live lab leaderboard/tournaments — no proxy metric, so this
 * tile can never duplicate Self-improvement's number again. There is no ELO
 * time series, so the sparkline stays empty (PulseTile hides it under 2 points)
 * and no delta is shown rather than a fabricated one.
 */
export function shapeCapabilityTile(snapshot: CapabilitySnapshot | null): PulseTileData {
  const top = snapshot?.topElo ?? null;
  return {
    title: "Capability",
    primary: top !== null ? String(Math.round(top)) : "—",
    caption: snapshot
      ? top !== null
        ? `${snapshot.topConfig ?? "top config"} · ${snapshot.ranked} ranked across ${snapshot.subjects} subject${snapshot.subjects === 1 ? "" : "s"} · ${snapshot.tournamentsCompleted} tournaments run`
        : "no configs ranked yet"
      : "no configs ranked yet",
    sparkPoints: [],
    // The standalone /leaderboard + /tournaments pages were retired in the dashboard
    // prune; the Lab is now the one surface that owns ELO/tournament results.
    deepLink: "/lab",
    tone: "accent",
  };
}

/**
 * Shape the Memory / Knowledge tile. Facts promoted to the typed graph.
 */
export function shapeMemoryTile(series: SeriesResponse | null): PulseTileData {
  const val = latestValue(series);
  return {
    title: "Memory & Knowledge",
    primary: formatValue("memory.facts", val),
    caption: "facts promoted to typed memory",
    delta: {
      value: deltaFromSeries(series),
      trend: trendFromDelta(deltaFromSeries(series)),
    },
    sparkPoints: (series?.points ?? []).map((p) => p.value),
    deepLink: "/knowledge",
    tone: "accent",
  };
}

/**
 * Shape the Reliability tile. Score is already 0..1; rendered as %.
 */
export function shapeReliabilityTile(series: SeriesResponse | null): PulseTileData {
  const val = latestValue(series);
  const tone = val >= 0.95 ? "ok" : val >= 0.8 ? "warn" : "danger";
  return {
    title: "Reliability",
    primary: formatValue("reliability.score", val),
    caption: "health score (100% = all clear)",
    delta: {
      value: deltaFromSeries(series),
      trend: trendFromDelta(deltaFromSeries(series)),
    },
    sparkPoints: (series?.points ?? []).map((p) => p.value),
    deepLink: "/reliability",
    tone,
  };
}
