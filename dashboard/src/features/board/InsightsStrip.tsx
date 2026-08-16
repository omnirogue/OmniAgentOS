"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Badge, Card, Pill, StatusDot } from "@/design";
import type { Agent, LiveBoardTask } from "@/features/collab/types";
import { apiUrl } from "@/lib/apiRoute";
import { API_BASE } from "@/lib/contracts";
import { fetchWithTimeout } from "@/lib/fetchTimeout";
import { startVisibilityPoll } from "@/lib/pollWhenVisible";
import { SETTLED_TASK_STATUSES } from "@/features/swarm/columns";
import type { ProviderHealthRow } from "@/features/swarm/types";
import styles from "./insights.module.css";

const POLL_MS = 20_000;

type TodaySummary = {
  started_today: number;
  completed_today: number;
};

type RemoteStat<T> =
  | { state: "loading"; value: null }
  | { state: "ready"; value: T }
  | { state: "unavailable"; value: null };

function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

async function fetchProviders(): Promise<ProviderHealthRow[]> {
  const response = await fetchWithTimeout(apiUrl(API_BASE, "/api/swarm/providers", "GET"), {
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`Provider health unavailable (${response.status})`);
  const payload = await response.json() as unknown;
  if (!Array.isArray(payload)) throw new Error("Provider health response is invalid");
  return payload as ProviderHealthRow[];
}

async function fetchToday(): Promise<TodaySummary> {
  const response = await fetchWithTimeout(apiUrl(API_BASE, "/api/dashboard/today", "GET"), {
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`Today summary unavailable (${response.status})`);
  const payload = await response.json() as Partial<TodaySummary>;
  if (!isNumber(payload.started_today) || !isNumber(payload.completed_today)) {
    throw new Error("Today summary response is invalid");
  }
  return { started_today: payload.started_today, completed_today: payload.completed_today };
}

function RemoteValue({ value }: { value: string }) {
  return <span className={styles.value}>{value}</span>;
}

export function StalenessToggle({
  hiddenCount,
  showStale,
  onToggle,
}: {
  hiddenCount: number;
  showStale: boolean;
  onToggle: () => void;
}) {
  if (hiddenCount === 0) return null;

  const label = showStale
    ? `${hiddenCount} stale shown`
    : `${hiddenCount} stale hidden`;

  return (
    <button
      type="button"
      className={styles.stalenessToggle}
      aria-pressed={showStale}
      onClick={onToggle}
    >
      <Pill tone={showStale ? "accent" : "neutral"}>{label}</Pill>
    </button>
  );
}

/** Toggle for the auto-discovered "[agent · external] host · tty" terminal-
 * session sighting cards (668/3,710 at filing — operator: "tasks that don't
 * exist"). Hidden by default; the count stays visible either way so the
 * hidden set is never silently absent. Renders even at zero so the operator
 * can see the filter exists and confirm nothing is hidden. */
export function ObservationalToggle({
  hiddenCount,
  showObservational,
  onToggle,
}: {
  hiddenCount: number;
  showObservational: boolean;
  onToggle: () => void;
}) {
  const label = showObservational
    ? `terminal cards: shown (${hiddenCount})`
    : `terminal cards: hidden (${hiddenCount})`;

  return (
    <button
      type="button"
      className={styles.stalenessToggle}
      aria-pressed={showObservational}
      onClick={onToggle}
    >
      <Pill tone={showObservational ? "accent" : "neutral"}>{label}</Pill>
    </button>
  );
}

export function InsightsStrip({
  tasks,
  agents,
  agentsError = null,
}: {
  tasks: LiveBoardTask[];
  agents: Agent[];
  agentsError?: string | null;
}) {
  const [providers, setProviders] = useState<RemoteStat<ProviderHealthRow[]>>({ state: "loading", value: null });
  const [today, setToday] = useState<RemoteStat<TodaySummary>>({ state: "loading", value: null });
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const request = ++generation.current;
    const [providerResult, todayResult] = await Promise.allSettled([fetchProviders(), fetchToday()]);
    if (request !== generation.current) return;

    setProviders(
      providerResult.status === "fulfilled"
        ? { state: "ready", value: providerResult.value }
        : { state: "unavailable", value: null },
    );
    setToday(
      todayResult.status === "fulfilled"
        ? { state: "ready", value: todayResult.value }
        : { state: "unavailable", value: null },
    );
  }, []);

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), POLL_MS);
  }, [refresh]);

  const busyAgents = agents.filter((agent) => agent.status === "busy").length;
  const idleAgents = agents.filter((agent) => agent.status === "idle").length;
  const activeSessions = providers.value?.reduce(
    (sum, row) => sum + (isNumber(row.active_sessions) ? row.active_sessions : 0),
    0,
  );
  const liveSessionsUnavailable = activeSessions === undefined;
  const doneCards = tasks.filter((task) => SETTLED_TASK_STATUSES.has(task.status)).length;
  const openCards = tasks.length - doneCards;

  return (
    <section className={styles.strip} aria-label="Board insights">
      <Card className={styles.statCard} padding="sm">
        <div className={styles.label}><StatusDot state={liveSessionsUnavailable || agentsError ? "warn" : "running"} />Live sessions</div>
        {liveSessionsUnavailable ? <RemoteValue value="unavailable" /> : <RemoteValue value={String(activeSessions)} />}
        {!liveSessionsUnavailable && !agentsError ? <span className={styles.detail}>{busyAgents} busy · {idleAgents} idle</span> : null}
        {agentsError && !liveSessionsUnavailable ? <span className={styles.detail}>agents unavailable</span> : null}
      </Card>
      <Card className={styles.statCard} padding="sm">
        <div className={styles.label}><StatusDot state={today.state === "unavailable" ? "warn" : "ok"} />Attempts today</div>
        {today.value ? (
          <span className={styles.detail}>Started {today.value.started_today} · Completed {today.value.completed_today} (UTC)</span>
        ) : <RemoteValue value={today.state === "unavailable" ? "unavailable" : "—"} />}
      </Card>
      <Card className={styles.statCard} padding="sm">
        <div className={styles.label}><StatusDot state="queued" />All cards</div>
        <div className={styles.counts}><Badge tone="queued">Open {openCards}</Badge><Badge tone="completed">Done {doneCards}</Badge></div>
      </Card>
    </section>
  );
}
