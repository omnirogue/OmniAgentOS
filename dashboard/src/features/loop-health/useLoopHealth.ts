"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Approval } from "@/lib/contracts";
import { startVisibilityPoll } from "@/lib/pollWhenVisible";
import type { RecentRunItem, Routine } from "@/features/routines/types";
import { deriveLoopHealth, type LoopHealthResult } from "./loopHealth";

/** `GET /api/routines/runs` response shape (cross-routine aggregate). */
type RecentRunsResponse = { runs: RecentRunItem[] };

/** Max the endpoint allows (`contracts/openapi.json`); requesting the ceiling
 * maximises the odds each loop's own recent runs survive the cross-routine
 * cut, since the aggregate has no per-routine filter. */
const RUNS_LIMIT = 500;

/**
 * Fetches the three endpoints the Loops card needs — `GET /api/routines`,
 * `GET /api/routines/runs`, `GET /api/approvals?state=pending` — and derives
 * the card model. No response shape is read from anywhere else, and none is
 * changed.
 *
 * On ANY fetch failure: set an error, clear the result, and render nothing
 * fixture-shaped. There is no fallback data path in this hook, by design —
 * see SUCCESS-CRITERIA-AND-TESTS.md §S8.6 / the no-fixture-fallback lane.
 */
export function useLoopHealth() {
  const [result, setResult] = useState<LoopHealthResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [routines, runsBody, approvals] = await Promise.all([
        api.get<Routine[]>("/api/routines"),
        api.get<RecentRunsResponse>(`/api/routines/runs?limit=${RUNS_LIMIT}`),
        api.get<Approval[]>("/api/approvals?state=pending"),
      ]);
      setResult(deriveLoopHealth(routines, runsBody.runs, approvals, new Date()));
      setError(null);
    } catch (reason) {
      setResult(null);
      setError(reason instanceof Error ? reason.message : "Could not load loop health.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), 30_000);
  }, [refresh]);

  return { result, loading, error, refresh };
}
