"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useReliabilityEvents } from "../../lib/useReliabilityEvents";
import { startVisibilityPoll } from "../../lib/pollWhenVisible";
import type { ReliabilityEventType } from "../../lib/reliabilityContracts";
import type {
  ApiAudit,
  ApiAutonomySetting,
  ApiImprovement,
  ApiReliabilityEvent,
  HealthSummary,
} from "../../lib/reliabilityContracts";
import {
  decideImprovement,
  fetchAudits,
  fetchAutonomy,
  fetchHealthSummary,
  fetchImprovements,
  fetchReliabilityEvents,
  ignoreReliabilityEvent,
  ReliabilityApiError,
  updateAutonomy,
} from "./api";

/** 5s safety poll when SSE is down or lagging (AC). */
export const RELIABILITY_POLL_MS = 5_000;

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof ReliabilityApiError || reason instanceof Error
    ? reason.message
    : fallback;
}

function useLiveRefresh(
  types: ReliabilityEventType[],
  refresh: () => Promise<void>,
  pollMs = RELIABILITY_POLL_MS,
) {
  const typesMemo = useMemo(() => types, [types.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps
  const { lastEvent, connected } = useReliabilityEvents(typesMemo);

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), pollMs);
  }, [refresh, pollMs]);

  useEffect(() => {
    if (lastEvent) void refresh();
  }, [lastEvent, refresh]);

  return { connected, lastEvent };
}

// ── Reliability page ────────────────────────────────────────────────────────

export function useReliabilityDashboard() {
  const [summary, setSummary] = useState<HealthSummary | null>(null);
  const [events, setEvents] = useState<ApiReliabilityEvent[]>([]);
  const [audits, setAudits] = useState<ApiAudit[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [severity, setSeverity] = useState("");
  const requestSeq = useRef(0);

  const refresh = useCallback(async () => {
    const seq = ++requestSeq.current;
    try {
      const [nextSummary, nextEvents, nextAudits] = await Promise.all([
        fetchHealthSummary(),
        fetchReliabilityEvents({
          q: query || undefined,
          severity: severity || undefined,
          limit: "50",
        }),
        fetchAudits(10),
      ]);
      if (seq !== requestSeq.current) return;
      setSummary(nextSummary);
      setEvents(nextEvents);
      setAudits(nextAudits);
      setError(null);
    } catch (reason) {
      if (seq !== requestSeq.current) return;
      setError(errorMessage(reason, "Failed to load reliability data"));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [query, severity]);

  const { connected } = useLiveRefresh(
    ["reliability.event", "audit.completed", "autonomy.changed"],
    refresh,
  );

  const ignoreEvent = useCallback(
    async (id: string) => {
      await ignoreReliabilityEvent(id);
      await refresh();
    },
    [refresh],
  );

  return {
    summary,
    events,
    audits,
    loading,
    error,
    connected,
    query,
    setQuery,
    severity,
    setSeverity,
    refresh,
    ignoreEvent,
  };
}

// ── Improvements page ───────────────────────────────────────────────────────

const QUEUE_STATUSES = "awaiting_human,panel_blocked,approved,judging,testing,proposed";

export type ImprovementTab = "queue" | "applied" | "monitoring" | "rolled_back" | "history";

function statusParamForTab(tab: ImprovementTab): string | undefined {
  switch (tab) {
    case "queue":
      return QUEUE_STATUSES;
    case "applied":
      return "applied,confirmed";
    case "monitoring":
      return "monitoring,applying";
    case "rolled_back":
      return "rolled_back,rolling_back";
    case "history":
      return undefined;
    default:
      return undefined;
  }
}

export function useImprovementsDashboard(tab: ImprovementTab) {
  const [items, setItems] = useState<ApiImprovement[]>([]);
  const [autonomy, setAutonomy] = useState<ApiAutonomySetting[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const refresh = useCallback(async () => {
    const seq = ++requestSeq.current;
    try {
      const [list, settings] = await Promise.all([
        fetchImprovements(statusParamForTab(tab)),
        fetchAutonomy(),
      ]);
      if (seq !== requestSeq.current) return;
      setItems(list);
      setAutonomy(settings);
      setError(null);
    } catch (reason) {
      if (seq !== requestSeq.current) return;
      setError(errorMessage(reason, "Failed to load improvements"));
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [tab]);

  const { connected } = useLiveRefresh(
    ["improvement.updated", "autonomy.changed"],
    refresh,
  );

  const act = useCallback(
    async (id: string, action: "approve" | "reject" | "apply" | "rollback") => {
      setActing(`${id}:${action}`);
      try {
        await decideImprovement(id, action);
        await refresh();
        setError(null);
      } catch (reason) {
        setError(errorMessage(reason, `Failed to ${action} improvement`));
      } finally {
        setActing(null);
      }
    },
    [refresh],
  );

  const saveAutonomy = useCallback(
    async (payload: {
      scope_type: string;
      scope_id?: string;
      mode: string;
      max_auto_risk: number;
    }) => {
      setActing("autonomy");
      try {
        await updateAutonomy(payload);
        await refresh();
        setError(null);
      } catch (reason) {
        setError(errorMessage(reason, "Failed to update autonomy"));
        throw reason;
      } finally {
        setActing(null);
      }
    },
    [refresh],
  );

  return {
    items,
    autonomy,
    loading,
    error,
    connected,
    acting,
    refresh,
    act,
    saveAutonomy,
  };
}

// ── Organization page ───────────────────────────────────────────────────────
// Note: Deleted useOrganizationDashboard hook. A future features/org/ tree view writes its own dashboard/hooks.

// ── Judges page ─────────────────────────────────────────────────────────────
// Note: Judge panel endpoints are not implemented in the backend (see FH-002).
// The useJudgesDashboard hook is retained for backward compatibility but returns
// an unavailable state. The judges page renders the unavailable message directly.

export function useJudgesDashboard() {
  return {
    panel: null,
    votes: [],
    stats: [],
    loading: false,
    error: null,
    connected: false,
    acting: false,
    refresh: async () => {},
    reseat: async () => {},
  };
}
