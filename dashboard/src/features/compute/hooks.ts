/**
 * React hook for the Observatory /compute surface. mountedRef guard mirrors
 * `features/testing/hooks.ts`'s `useTestObsOverview` exactly — a slow fetch
 * can never set state after unmount — with two additions on top:
 *
 *   1. A `setInterval` driving the 15s live poll the page's own caption
 *      promises ("live snapshot · refreshes every 15s", Rule 6 — the
 *      caption must stay true). A background poll keeps the last-good
 *      `data` on screen rather than re-flashing the loading skeleton every
 *      15s; only the very first fetch (before any data has ever landed)
 *      renders as "loading".
 *
 *   2. Behavior contract for a failed poll (UI-1 review fix), pinned
 *      executable-y in the co-located `hooks.test.ts` (UI-5) and restated
 *      here for the reader:
 *        - a poll failing AFTER data has landed at least once is a
 *          TRANSIENT BLIP: `data` and `status` are left exactly as they
 *          were (status stays "ready"), only `degraded` flips true and
 *          `error` carries the failure's message. A one-off network hiccup
 *          must never wipe a live fleet into ErrorState.
 *        - a poll failing BEFORE data has ever landed is a genuine outage:
 *          `data` stays/becomes null and `status` becomes "error".
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchComputeEstate } from "./client";
import type { ComputeEstate } from "./types";

export type AsyncStatus = "loading" | "ready" | "error";

export type UseComputeEstateResult = {
  data: ComputeEstate | null;
  status: AsyncStatus;
  /**
   * True when the MOST RECENT poll failed but a prior successful fetch's
   * data is still on screen (see the file-level doc comment for the full
   * behavior contract). The page renders a small muted "last update
   * failed — retrying" note off this flag — neutral tone, never a danger
   * takeover, since `data` is still trustworthy.
   */
  degraded: boolean;
  error: string | null;
  refresh: () => void;
};

/** The page's stated poll cadence. */
export const COMPUTE_POLL_MS = 15_000;

export function useComputeEstate(pollMs: number = COMPUTE_POLL_MS): UseComputeEstateResult {
  const [data, setData] = useState<ComputeEstate | null>(null);
  const [status, setStatus] = useState<AsyncStatus>("loading");
  const [degraded, setDegraded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const hasLoadedRef = useRef(false);

  const load = useCallback(() => {
    // Only the very first fetch shows the loading skeleton — a background
    // 15s poll must never blank out an already-live snapshot.
    if (!hasLoadedRef.current) setStatus("loading");
    fetchComputeEstate().then(
      (res) => {
        if (!mountedRef.current) return;
        hasLoadedRef.current = true;
        setData(res);
        setStatus("ready");
        setDegraded(false);
        setError(null);
      },
      (err: unknown) => {
        if (!mountedRef.current) return;
        const message = err instanceof Error ? err.message : String(err);
        setError(message);
        if (hasLoadedRef.current) {
          // Transient blip: keep the last-good snapshot exactly as it was.
          setDegraded(true);
          setStatus("ready");
        } else {
          // Never had data — this IS an outage from the caller's perspective.
          setData(null);
          setStatus("error");
          setDegraded(false);
        }
      },
    );
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    load();
    const timer = setInterval(load, pollMs);
    return () => {
      mountedRef.current = false;
      clearInterval(timer);
    };
  }, [load, pollMs]);

  return { data, status, degraded, error, refresh: load };
}

/**
 * The subset of `UseComputeEstateResult` the display components need
 * (UI-2 review fix): `useComputeEstate()` is now called exactly ONCE, at
 * page.tsx, and its `data`/`status`/`error`/`refresh` are passed down as
 * props to `ComputeSummaryRow`/`MachineGrid`/`RunnersStrip` — three
 * concurrent pollers hitting the same envelope collapse into one. `degraded`
 * is deliberately NOT part of this shape: only the page itself renders the
 * "last update failed" note, once, in the header area.
 */
export type ComputeEstateViewProps = Pick<UseComputeEstateResult, "data" | "status" | "error" | "refresh">;
