"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { EVENTS_BASE } from "../../lib/contracts";
import { startVisibilityPoll } from "../../lib/pollWhenVisible";
import { revenue, RevenueApiError } from "./api";
import { USE_REVENUE_FIXTURES } from "./fixtures";
import type { RevenueDay } from "./contracts";

// Revenue is a SLOW surface: the collectors refresh the DB hourly and the
// dashboard reads it in ~10ms. So we fetch ONCE on mount, refresh on the
// `revenue.updated` SSE push, and keep only a long safety interval (15 min) as
// a backstop for a browser whose SSE never connects — never a 60s poll.
const SAFETY_POLL_MS = 15 * 60_000;

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof RevenueApiError || reason instanceof Error ? reason.message : fallback;
}

/** Best-effort live refresh if a "revenue.updated" SSE event type ever exists
 * server-side — optional per the brief, since the data is only hourly. Not
 * part of the frozen EVENT_TYPES union in lib/contracts.ts, so this opens its
 * own EventSource (mirrors features/steward/hooks.ts's useStewardUpdates)
 * rather than touching that frozen file; harmless no-op if the event never
 * fires. */
function useRevenueSseRefresh(refresh: () => Promise<void>) {
  useEffect(() => {
    if (USE_REVENUE_FIXTURES) return;
    if (typeof EventSource === "undefined") return;
    let source: EventSource | null = null;
    try {
      source = new EventSource(`${EVENTS_BASE}/api/events?types=revenue.updated`);
      const update = () => void refresh();
      source.addEventListener("revenue.updated", update);
      // Ignore transport errors silently — the SSE push is the primary freshness
      // signal, with the 15-min safety interval as the only backstop.
      source.onerror = () => {};
    } catch {
      /* SSE is optional; the 15-min safety interval is the fallback. */
    }
    return () => source?.close();
  }, [refresh]);
}

/** Loads one day's revenue/P&L snapshot from the DB-cached facts and keeps it
 * fresh with the `revenue.updated` SSE push plus a 15-minute safety backstop —
 * the data only changes hourly, so short polling was pure waste. */
export function useRevenueDay(day?: string) {
  const [data, setData] = useState<RevenueDay | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const dayRef = useRef(day);
  dayRef.current = day;

  const refresh = useCallback(async () => {
    try {
      const next = await revenue(dayRef.current);
      setData(next);
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load revenue"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    void refresh();
    return startVisibilityPoll(() => void refresh(), SAFETY_POLL_MS);
  }, [refresh, day]);

  useRevenueSseRefresh(refresh);

  return { data, loading, error, refresh };
}
