"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { EVENTS_BASE } from "../../lib/contracts";
import { startVisibilityPoll } from "../../lib/pollWhenVisible";
import { banking, CashApiError } from "./api";
import type { CashDay } from "./contracts";

// Cash / banking is a SLOW surface: the daily collector writes the DB and the
// dashboard reads it in ~10ms. Fetch ONCE on mount, refresh on a `bank.updated`
// SSE push, and keep only a 15-min safety interval as a backstop — no 60s poll.
const SAFETY_POLL_MS = 15 * 60_000;

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof CashApiError || reason instanceof Error ? reason.message : fallback;
}

/** Best-effort live refresh if a "bank.updated" SSE event ever exists server-side
 * — optional, since the data is only daily. Mirrors features/revenue/hooks.ts's
 * useRevenueSseRefresh; a harmless no-op if the event never fires. */
function useCashSseRefresh(refresh: () => Promise<void>) {
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    let source: EventSource | null = null;
    try {
      source = new EventSource(`${EVENTS_BASE}/api/events?types=bank.updated`);
      const update = () => void refresh();
      source.addEventListener("bank.updated", update);
      source.onerror = () => {};
    } catch {
      /* SSE is optional; the 15-min safety interval is the fallback. */
    }
    return () => source?.close();
  }, [refresh]);
}

/** Loads one day's cash / banking snapshot from the DB-cached facts and keeps it
 * fresh with the `bank.updated` SSE push plus a 15-minute safety backstop — the
 * data only changes daily, so short polling was pure waste. */
export function useCashDay(day?: string) {
  const [data, setData] = useState<CashDay | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const dayRef = useRef(day);
  dayRef.current = day;

  const refresh = useCallback(async () => {
    try {
      const next = await banking(dayRef.current);
      setData(next);
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load cash"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    void refresh();
    return startVisibilityPoll(() => void refresh(), SAFETY_POLL_MS);
  }, [refresh, day]);

  useCashSseRefresh(refresh);

  return { data, loading, error, refresh };
}
