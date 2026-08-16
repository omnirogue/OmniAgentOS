"use client";

import { useCallback, useEffect, useState } from "react";
import { startVisibilityPoll } from "@/lib/pollWhenVisible";
import type { StatusResponse } from "../types";

/** Operator wants this to beat `osq` on density and freshness — 20s poll,
 * paused while the tab is hidden (same idiom as StatusPill/useLoopHealth). */
const POLL_MS = 20_000;

/**
 * Fetches `GET /api/local/status` (a same-origin Next server route — no
 * `apiUrl`/session-token proxying needed, it never leaves this box) and
 * polls it every 20s while the tab is visible. On a fetch/parse failure the
 * previous good `data` is kept on screen (so a single blip doesn't blank a
 * dashboard that's otherwise fine) while `error` carries the reason.
 *
 * F04: a failed poll after a successful load must not render silently as if
 * the (now stale) `data` were still current. `lastUpdated` is the epoch ms of
 * the last SUCCESSFUL fetch; `refreshError` is the reason the most recent
 * attempt failed, cleared on the next success. Callers render a stale banner
 * whenever `refreshError` is set while `data` is still present.
 */
export function useStatus() {
  const [data, setData] = useState<StatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/local/status", { cache: "no-store" });
      if (!res.ok) throw new Error(`status endpoint returned ${res.status}`);
      const body = (await res.json()) as StatusResponse;
      setData(body);
      setError(null);
      setRefreshError(null);
      setLastUpdated(Date.now());
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "Could not load status.";
      setError(message);
      // Deliberately unconditional: on the very first (no-data-yet) failure
      // this is equivalent to `error`; once `data` exists it is what tells a
      // later poll failure apart from "nothing has ever loaded".
      setRefreshError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), POLL_MS);
  }, [refresh]);

  return { data, loading, error, refresh, lastUpdated, refreshError };
}
