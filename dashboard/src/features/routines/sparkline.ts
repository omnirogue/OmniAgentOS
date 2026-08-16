/** Acceptance-rate sparkline data shaping.
 *
 * The design ``Sparkline`` primitive expects a flat ``number[]`` of points.
 * The Loops page computes a per-loop acceptance rate over a rolling window
 * of its most recent runs, oldest-first, so the sparkline visualizes how
 * acceptance has drifted over the last N firings — not just the current
 * lifetime rate.
 *
 * Pure, synchronous, zero dependencies. Covered by
 * ``__tests__/sparkline.test.ts``.
 */

import type { RoutineRun } from "./types";

/** Rolling-window acceptance rates over ``runs``, oldest-first, one point
 * per run (the acceptance rate INCLUDING that run). Runs with a null
 * ``accepted`` are skipped. Returns ``[]`` if no settled runs exist.
 *
 * Example: runs [accepted, rejected, accepted] → [1.0, 0.5, 0.667].
 */
export function rollingAcceptanceRates(runs: RoutineRun[]): number[] {
  // ``runs`` from the API is newest-first; we want oldest-first for the
  // rolling window.
  const ordered = [...runs].reverse();
  const points: number[] = [];
  let total = 0;
  let accepted = 0;
  for (const run of ordered) {
    if (run.accepted === null) continue;
    total += 1;
    if (run.accepted) accepted += 1;
    points.push(total === 0 ? 0 : accepted / total);
  }
  return points;
}

/** Cross-bucket helper: given recent runs from the aggregate endpoint
 * (which carry ``routine_id``), group them by routine and compute rolling
 * acceptance-rate points per routine. Oldest-first within each bucket. */
export function bucketAcceptanceRates<A extends { routine_id: string; accepted: boolean | null }>(
  runs: A[],
): Map<string, number[]> {
  const byRoutine = new Map<string, A[]>();
  // Aggregate is newest-first; reverse so oldest-first when bucketing.
  for (const run of [...runs].reverse()) {
    const bucket = byRoutine.get(run.routine_id) ?? [];
    bucket.push(run);
    byRoutine.set(run.routine_id, bucket);
  }
  const out = new Map<string, number[]>();
  for (const [routineId, bucket] of byRoutine) {
    const points: number[] = [];
    let total = 0;
    let accepted = 0;
    for (const run of bucket) {
      if (run.accepted === null) continue;
      total += 1;
      if (run.accepted) accepted += 1;
      points.push(total === 0 ? 0 : accepted / total);
    }
    if (points.length > 0) out.set(routineId, points);
  }
  return out;
}

/** Pick the right ``tone`` for the sparkline given the CURRENT (final-point)
 * acceptance rate. ``>=0.5`` → ``ok``, ``<0.5`` → ``danger``, no data →
 * ``neutral``-equivalent ``accent``. Mirrors ``acceptanceRateTone`` in
 * ``format.ts`` but applied to the trailing point, not the rollup. */
export function sparklineToneForPoints(
  points: number[],
): "ok" | "danger" | "accent" {
  if (points.length === 0) return "accent";
  const last = points[points.length - 1]!;
  return last >= 0.5 ? "ok" : "danger";
}
