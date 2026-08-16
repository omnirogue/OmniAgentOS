"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { listAccounts } from "@/features/accounts/client";
import { api, ApiError } from "@/lib/api";
import { startVisibilityPoll } from "@/lib/pollWhenVisible";
import type { Session } from "@/lib/contracts";
import {
  fetchSwarmFleet,
  fetchSwarmOverview,
  fetchSwarmProviders,
  fetchSwarmTeam,
} from "./client";
import type {
  ProviderAccount,
  ProviderHealthRow,
  SwarmFleet,
  SwarmOverview,
  SwarmTeam,
} from "./types";
import type { SwarmSseRow } from "./useSwarmEvents";

/** How many tasks/hour samples the overview poll retains for the sparkline —
 * ~10 minutes of history at the 15s cadence. */
const OVERVIEW_HISTORY_MAX = 40;

/** Session states that are still live — the only ones the Command Center shows. */
const ACTIVE_SESSION_STATES: ReadonlySet<Session["state"]> = new Set([
  "starting",
  "running",
  "awaiting_approval",
  "resuming",
]);

export function isActiveSession(session: Session): boolean {
  return ACTIVE_SESSION_STATES.has(session.state);
}

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

/**
 * The WP6a fleet endpoint, polled every 30s. Resolves to `null` (endpoint not
 * built yet, a 404) without erroring — the page then derives the fleet from
 * board cards. A real outage sets `error` so the strip can note staleness.
 */
export function useSwarmFleet() {
  const [fleet, setFleet] = useState<SwarmFleet | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const gen = ++generation.current;
    try {
      const next = await fetchSwarmFleet();
      if (gen !== generation.current) return;
      setFleet(next);
      setError(null);
    } catch (reason) {
      if (gen !== generation.current) return;
      setError(errorMessage(reason, "Failed to load the swarm fleet"));
    } finally {
      if (gen === generation.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), 30_000);
  }, [refresh]);

  return { fleet, loading, error, refresh };
}

/**
 * Fleet-level swarm metrics (`GET /api/swarm/overview`, C2), polled every
 * `pollMs` (default 15s) with a generation guard so a slow response can never
 * clobber a newer one (the same house pattern as `useSwarmFleet`). When the C3
 * SSE stream is connected the page stretches this to 30s — the debounced
 * `swarm.event` refresh hints carry the timely updates, so the poll becomes a
 * slow safety net rather than the primary cadence. Resolves to `null` when the
 * endpoint is absent (404) so the ThroughputPanel shows its EmptyState instead
 * of an error. Each successful poll appends `throughput.tasks_per_hour` to a
 * bounded in-memory ring buffer (`history`) — a cheap, honest tasks/hour
 * sparkline series without any server-side history table.
 */
export function useSwarmOverview(pollMs = 15_000) {
  const [overview, setOverview] = useState<SwarmOverview | null>(null);
  const [history, setHistory] = useState<number[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hasLoaded, setHasLoaded] = useState(false);
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const gen = ++generation.current;
    try {
      const next = await fetchSwarmOverview();
      if (gen !== generation.current) return;
      setOverview(next);
      if (next) {
        setHistory((prev) => {
          const appended = [...prev, next.throughput.tasks_per_hour];
          return appended.length > OVERVIEW_HISTORY_MAX
            ? appended.slice(appended.length - OVERVIEW_HISTORY_MAX)
            : appended;
        });
      }
      setHasLoaded(true);
      setError(null);
    } catch (reason) {
      if (gen !== generation.current) return;
      setError(errorMessage(reason, "Failed to load swarm metrics"));
    } finally {
      if (gen === generation.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), pollMs);
  }, [refresh, pollMs]);

  const stale = Boolean(error) && hasLoaded;
  return { overview, history, loading, error, hasLoaded, stale, refresh };
}

/**
 * Live team board (`GET /api/swarm/team`) — leaders, formations, running agents,
 * recent spawns. Polled every 10s (or 20s when SSE is connected). Cheap: no
 * token streams.
 */
export function useSwarmTeam(pollMs = 10_000) {
  const [team, setTeam] = useState<SwarmTeam | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hasLoaded, setHasLoaded] = useState(false);
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const gen = ++generation.current;
    try {
      const next = await fetchSwarmTeam();
      if (gen !== generation.current) return;
      setTeam(next);
      setHasLoaded(true);
      setError(null);
    } catch (reason) {
      if (gen !== generation.current) return;
      setError(errorMessage(reason, "Failed to load team board"));
    } finally {
      if (gen === generation.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), pollMs);
  }, [refresh, pollMs]);

  const stale = Boolean(error) && hasLoaded;
  return { team, loading, error, hasLoaded, stale, refresh };
}

/**
 * Active bridge/external sessions for the TerminalGrid — `GET /api/sessions`
 * filtered to live states, polled every 10s with an in-flight + generation
 * guard so a slow response can never clobber a newer one. `GET /api/sessions`
 * is token-gated, so a browser with no local session token gets a 401 on every
 * poll: that is surfaced as `unauthorized` (a calm, permanent condition) rather
 * than an alarming error, mirroring the Sessions page.
 */
export function useSwarmTerminals() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [unauthorized, setUnauthorized] = useState(false);
  const [hasLoaded, setHasLoaded] = useState(false);
  const generation = useRef(0);
  const inFlight = useRef(false);
  const pendingRerun = useRef(false);

  const refresh = useCallback(async () => {
    if (inFlight.current) {
      pendingRerun.current = true;
      return;
    }
    inFlight.current = true;
    const gen = ++generation.current;
    try {
      const next = await api.sessions();
      if (gen !== generation.current) return;
      setSessions(next.filter(isActiveSession));
      setHasLoaded(true);
      setError(null);
      setUnauthorized(false);
    } catch (reason) {
      if (gen !== generation.current) return;
      if (reason instanceof ApiError && reason.status === 401) {
        setUnauthorized(true);
        setError(null);
      } else {
        setUnauthorized(false);
        setError(errorMessage(reason, "Unable to load sessions."));
      }
    } finally {
      if (gen === generation.current) setLoading(false);
      inFlight.current = false;
      if (pendingRerun.current) {
        pendingRerun.current = false;
        void refresh();
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), 10_000);
  }, [refresh]);

  // Stale = we have shown data before but the latest poll failed.
  const stale = Boolean(error) && hasLoaded;
  return { sessions, loading, error, unauthorized, hasLoaded, stale, refresh };
}

/** `GET /api/accounts` row → the unified ProviderHealthRow the panel renders.
 * The fallback cannot compute the durable fields (`reset_in_seconds`,
 * `active_sessions`, `max_inflight`) — those are the primary route's value-add —
 * so they are null; the panel still ticks a live cooldown off `cooldown_until`.
 */
function accountToProviderRow(account: ProviderAccount): ProviderHealthRow {
  return {
    provider: account.provider ?? "claude",
    account_id: account.id,
    display_name: account.label,
    status: account.status,
    cooldown_until: account.cooldown_until ?? null,
    reset_in_seconds: null,
    active_sessions: null,
    max_inflight: null,
    status_detail: account.status_detail ?? null,
  };
}

/**
 * Provider/account health for the ProviderHealthPanel, polled every 30s.
 * PRIMARY source is `GET /api/swarm/providers` (C4 — durable rate-limit state,
 * reset countdowns, inflight, and the account-less CLI-provider rows); on a
 * 404/unavailable (the route not deployed) it falls back to `GET /api/accounts`
 * exactly as before, normalized to the same `ProviderHealthRow` shape so the
 * panel renders one uniform row type. `source` tells the page which path won.
 */
export function useProviderHealth() {
  const [rows, setRows] = useState<ProviderHealthRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [source, setSource] = useState<"providers" | "accounts">("providers");
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const gen = ++generation.current;
    try {
      const primary = await fetchSwarmProviders();
      if (gen !== generation.current) return;
      if (primary) {
        setRows(primary);
        setSource("providers");
      } else {
        // 404 / network — degrade to the accounts endpoint (C4 plan bullet).
        const accounts = (await listAccounts()) as ProviderAccount[];
        if (gen !== generation.current) return;
        setRows(accounts.map(accountToProviderRow));
        setSource("accounts");
      }
      setHasLoaded(true);
      setError(null);
    } catch (reason) {
      if (gen !== generation.current) return;
      setError(errorMessage(reason, "Failed to load provider health"));
    } finally {
      if (gen === generation.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), 30_000);
  }, [refresh]);

  const stale = Boolean(error) && hasLoaded;
  return { rows, loading, error, hasLoaded, stale, source, refresh };
}

// ── C3 SSE refresh router ────────────────────────────────────────────────────

export type SwarmRefreshTarget =
  | "overview"
  | "fleet"
  | "terminals"
  | "providers"
  | "team";

export interface SwarmRefreshTargets {
  refreshOverview: () => void | Promise<void>;
  refreshFleet: () => void | Promise<void>;
  refreshTerminals: () => void | Promise<void>;
  refreshProviders: () => void | Promise<void>;
  refreshTeam?: () => void | Promise<void>;
}

/** `swarm.event` action → which REST reads a refresh HINT should refetch. An
 * action absent here (unknown/new, or the synthetic `resync`) refreshes
 * everything — a hint can never do harm, and the per-panel polls are the floor.
 */
const ACTION_TARGETS: Record<string, SwarmRefreshTarget[]> = {
  plan_created: ["fleet", "overview", "team"],
  run_started: ["fleet", "overview", "team"],
  run_completed: ["fleet", "overview", "team"],
  run_failed: ["fleet", "overview", "team"],
  slot_opened: ["fleet", "overview", "team"],
  resize: ["fleet", "overview", "team"],
  task_assigned: ["overview", "fleet", "terminals", "team"],
  task_completed: ["overview", "fleet", "terminals", "team"],
  worker_spawned: ["team", "overview", "terminals"],
  leader_update: ["team", "overview"],
  review_confirmed: ["overview", "fleet", "team"],
  review_denied: ["overview", "fleet", "team"],
  task_blocked: ["overview", "fleet", "team"],
  task_split: ["overview", "fleet", "team"],
  approval_parked: ["overview", "fleet", "terminals", "team"],
  merge_started: ["overview", "fleet", "team"],
  provider_switched: ["providers", "overview", "terminals", "team"],
  rate_limit: ["providers", "overview"],
  rate_limit_stall: ["providers", "overview"],
};

const ALL_TARGETS: SwarmRefreshTarget[] = [
  "overview",
  "fleet",
  "terminals",
  "providers",
  "team",
];

/** 1s coalescing window (plan C3: "1s debounce → refetch ... as appropriate by
 * action name"). Multiple hints inside the window union their targets and fire
 * ONE refetch each. */
export const SWARM_REFRESH_DEBOUNCE_MS = 1000;

/**
 * Turn the `swarm.event` SSE stream into DEBOUNCED refresh hints. Each frame's
 * `action` selects which REST reads to refetch (`ACTION_TARGETS`); hints landing
 * within `debounceMs` of each other coalesce (their targets union) and fire one
 * refetch per target. REST stays the source of truth — this never mutates page
 * state directly, it only nudges the existing polling hooks to refresh early.
 */
export function useSwarmEventRouter(
  lastEvent: SwarmSseRow | null,
  targets: SwarmRefreshTargets,
  debounceMs = SWARM_REFRESH_DEBOUNCE_MS,
): void {
  const targetsRef = useRef(targets);
  targetsRef.current = targets;
  const pending = useRef<Set<SwarmRefreshTarget>>(new Set());
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastHandledId = useRef<number>(-1);

  useEffect(() => {
    if (!lastEvent) return;
    const isResync = lastEvent.type === "resync";
    // Guard against the effect re-firing on the same frame (a resync always
    // re-processes — it means "you missed some, refetch all").
    if (!isResync && lastEvent.id === lastHandledId.current) return;
    lastHandledId.current = lastEvent.id;

    const forThis = isResync ? ALL_TARGETS : ACTION_TARGETS[lastEvent.action] ?? ALL_TARGETS;
    for (const target of forThis) pending.current.add(target);

    if (timer.current != null) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      const toRun = pending.current;
      pending.current = new Set();
      timer.current = null;
      const fns = targetsRef.current;
      if (toRun.has("overview")) void fns.refreshOverview();
      if (toRun.has("fleet")) void fns.refreshFleet();
      if (toRun.has("terminals")) void fns.refreshTerminals();
      if (toRun.has("providers")) void fns.refreshProviders();
      if (toRun.has("team") && fns.refreshTeam) void fns.refreshTeam();
    }, debounceMs);
  }, [lastEvent, debounceMs]);

  useEffect(
    () => () => {
      if (timer.current != null) clearTimeout(timer.current);
    },
    [],
  );
}
