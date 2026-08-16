"use client";

import { useCallback, useEffect, useState } from "react";
import { EVENTS_BASE } from "../../lib/contracts";
import { useEventChannel } from "../../lib/useEventChannel";
import {
  ackAlert,
  ackBriefing,
  approveSuggestion,
  dismissSuggestion,
  fetchAlertCount,
  fetchAlerts,
  fetchBriefingHistory,
  fetchCommsMessages,
  fetchGoalDetail,
  fetchGoals,
  fetchLatestBriefing,
  fetchSuggestions,
  generateBriefing,
  StewardApiError,
} from "./api";
import { USE_STEWARD_FIXTURES } from "./fixtures";
import type {
  Alert,
  ApproveSuggestionResult,
  Briefing,
  BriefingSummary,
  CommsMessage,
  CommsMessageFilters,
  Goal,
  GoalDetail,
  Suggestion,
} from "./types";

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof StewardApiError || reason instanceof Error ? reason.message : fallback;
}

/** Re-fetch on the Steward SSE event types. Mirrors features/collab/hooks.ts's
 * useCollabUpdates: EventSource on GET /api/events?types=..., closed on unmount. */
function useStewardUpdates(types: string[], refresh: () => Promise<void>) {
  const typesKey = types.join(",");
  useEffect(() => {
    if (USE_STEWARD_FIXTURES) return;
    const source = new EventSource(`${EVENTS_BASE}/api/events?types=${encodeURIComponent(typesKey)}`);
    const update = () => {
      void refresh();
    };
    typesKey.split(",").forEach((type) => source.addEventListener(type, update));
    source.onmessage = update;
    return () => source.close();
  }, [refresh, typesKey]);
}

/** Always-on variant of `useStewardUpdates` for the AppShell topbar bell, which
 * mounts on EVERY page. Rides the ONE shared per-tab EventSource owned by
 * `EventStreamProvider` (T1.4) instead of opening a connection of its own —
 * browsers cap HTTP/1.1 at 6 per origin, and the always-on hooks were burning
 * three of those before a page rendered anything. Page-scoped steward hooks
 * keep `useStewardUpdates` until the later sweep. */
function useSharedStewardUpdates(types: string[], refresh: () => Promise<void>) {
  // `types` is re-created per render by callers; useEventChannel keys off the
  // joined string, so the subscription is stable.
  const { lastEvent } = useEventChannel(types);
  useEffect(() => {
    if (USE_STEWARD_FIXTURES) return;
    if (!lastEvent) return;
    void refresh();
  }, [lastEvent, refresh]);
}

export function useBriefing() {
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [history, setHistory] = useState<Briefing[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [generating, setGenerating] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [latest, past] = await Promise.all([fetchLatestBriefing(), fetchBriefingHistory()]);
      setBriefing(latest);
      setHistory(past);
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load the briefing"));
    } finally {
      setLoading(false);
    }
  }, []);

  const generate = useCallback(async (): Promise<BriefingSummary> => {
    setGenerating(true);
    try {
      const result = await generateBriefing(false);
      await refresh();
      return result;
    } finally {
      setGenerating(false);
    }
  }, [refresh]);

  const ack = useCallback(
    async (id: string) => {
      const updated = await ackBriefing(id);
      setBriefing((current) => (current && current.id === id ? updated : current));
      setHistory((current) => current.map((row) => (row.id === id ? updated : row)));
      return updated;
    },
    [],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useStewardUpdates(["briefing.ready"], refresh);

  return { briefing, history, loading, error, generating, refresh, generate, ack };
}

export function useGoals() {
  const [goals, setGoals] = useState<Goal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setGoals(await fetchGoals());
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load goals"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useStewardUpdates(["goal.metric"], refresh);

  return { goals, loading, error, refresh };
}

export function useGoalDetail(id: string | null) {
  const [goal, setGoal] = useState<GoalDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!id) {
      setGoal(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      setGoal(await fetchGoalDetail(id));
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load this goal"));
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useStewardUpdates(["goal.metric", "suggestion.updated"], refresh);

  return { goal, loading, error, refresh };
}

export function useCommsMessages(filters: CommsMessageFilters) {
  const [messages, setMessages] = useState<CommsMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const filterKey = JSON.stringify(filters);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setMessages(await fetchCommsMessages(JSON.parse(filterKey) as CommsMessageFilters));
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load messages"));
    } finally {
      setLoading(false);
    }
  }, [filterKey]);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useStewardUpdates(["comms.message"], refresh);

  return { messages, loading, error, refresh };
}

export function useSuggestions(state?: string) {
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setSuggestions(await fetchSuggestions(state));
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load suggestions"));
    } finally {
      setLoading(false);
    }
  }, [state]);

  const approve = useCallback(
    async (id: string, approvedBy: string): Promise<ApproveSuggestionResult> => {
      const result = await approveSuggestion(id, approvedBy);
      await refresh();
      return result;
    },
    [refresh],
  );

  const dismiss = useCallback(
    async (id: string, decidedBy: string, reason?: string): Promise<Suggestion> => {
      const result = await dismissSuggestion(id, decidedBy, reason);
      await refresh();
      return result;
    },
    [refresh],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useStewardUpdates(["suggestion.updated"], refresh);

  return { suggestions, loading, error, refresh, approve, dismiss };
}

export function useAlerts(state?: string) {
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setAlerts(await fetchAlerts(state));
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load alerts"));
    } finally {
      setLoading(false);
    }
  }, [state]);

  const ack = useCallback(async (id: number, by?: string) => {
    const updated = await ackAlert(id, by);
    setAlerts((current) => current.map((row) => (row.id === id ? updated : row)));
    return updated;
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useStewardUpdates(["alert.created"], refresh);

  return { alerts, loading, error, refresh, ack };
}

const ALERT_COUNT_EVENT_TYPES = ["alert.created"];

/** Powers the AppShell topbar bell: open-alert count, refreshed on alert.created. */
export function useAlertCount() {
  const [count, setCount] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setCount((await fetchAlertCount()).open);
      setError(null);
    } catch (reason) {
      setError(errorMessage(reason, "Failed to load alert count"));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useSharedStewardUpdates(ALERT_COUNT_EVENT_TYPES, refresh);

  return { count, error, refresh };
}
