"use client";

import { type CSSProperties, useCallback, useEffect, useRef, useState } from "react";
import { Badge, Pill } from "@/design";
import { taskStaleness } from "@/features/board/staleness";
import styles from "@/features/board/board.module.css";
import type { TaskCategory } from "@/features/collab/types";
import { startVisibilityPoll } from "@/lib/pollWhenVisible";
import { fetchSwarmRun } from "./client";
import { TERMINAL_COLUMNS } from "./columns";
import { elapsedLabel, flattenAttempts, liveSessionCount, type SwarmRunGroup } from "./runAggregate";
import type { SwarmRunDetail } from "./types";

/**
 * ONE card for a whole swarm run (user directive — never the member tasks).
 * Renders instantly from the board-derived aggregate (progress, preview). Live
 * cards enrich and poll automatically; terminal cards fetch detail only after
 * the operator explicitly opens them.
 */

const DETAIL_REFRESH_MS = 15_000;
const LIVE_RUN_STATES = new Set(["queued", "running", "awaiting_approval"]);

export function isLiveSwarmRun(group: SwarmRunGroup): boolean {
  const cards = [group.root, ...group.members].filter(
    (task): task is NonNullable<typeof task> => task !== null,
  );
  if (cards.some((task) => task.status === "awaiting_approval")) return true;
  // Terminal aggregate truth wins over stale run_state values on old cards.
  // Accepted finalize window: if every member is done while run status still
  // says running, the terminal aggregate deliberately stops polling.
  if (TERMINAL_COLUMNS.has(group.column)) return false;
  return (
    group.running > 0 ||
    cards.some((task) => LIVE_RUN_STATES.has(task.run_state ?? ""))
  );
}

interface SwarmRunDetailState {
  detail: SwarmRunDetail | null;
  load: () => Promise<SwarmRunDetail | null>;
}

function useSwarmRunDetailLoader(runId: string, live: boolean): SwarmRunDetailState {
  const [detail, setDetail] = useState<SwarmRunDetail | null>(null);
  const mountedRef = useRef(true);
  const inFlightRef = useRef<Promise<SwarmRunDetail | null> | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const load = useCallback((): Promise<SwarmRunDetail | null> => {
    if (inFlightRef.current) return inFlightRef.current;
    const request = fetchSwarmRun(runId)
      .then((next) => {
        if (mountedRef.current && next) setDetail(next);
        return next;
      })
      .catch(() => null)
      .finally(() => {
        if (inFlightRef.current === request) inFlightRef.current = null;
      });
    inFlightRef.current = request;
    return request;
  }, [runId]);

  useEffect(() => {
    if (!live) return;
    void load();
    return startVisibilityPoll(() => {
      void load();
    }, DETAIL_REFRESH_MS);
  }, [live, load]);

  return { detail, load };
}

/** Backwards-compatible detail-only hook for non-interactive consumers. */
export function useSwarmRunDetail(runId: string, live: boolean): SwarmRunDetail | null {
  return useSwarmRunDetailLoader(runId, live).detail;
}

function attemptAgentLabels(detail: SwarmRunDetail | null): string[] {
  const labels: string[] = [];
  const attempts = [...flattenAttempts(detail?.attempts)].sort((a, b) =>
    Number(a.end_reason != null) - Number(b.end_reason != null),
  );
  for (const attempt of attempts) {
    const provider = attempt.provider?.trim();
    const model = attempt.model?.trim();
    const label = provider && model ? `${provider}/${model}` : (model || provider);
    if (label && !labels.includes(label)) labels.push(label);
  }
  return labels.slice(0, 4);
}

function latestTaskUpdate(group: SwarmRunGroup): string | null {
  let latest: { iso: string; time: number } | null = null;
  for (const task of [group.root, ...group.members]) {
    if (!task) continue;
    const time = Date.parse(task.updated_at);
    if (Number.isFinite(time) && (!latest || time > latest.time)) {
      latest = { iso: task.updated_at, time };
    }
  }
  return latest?.iso ?? null;
}

export function SwarmRunCard({
  group,
  category,
  onOpen,
  onOpenUpdate,
  onOpenUnavailable,
  onPause,
  pausing,
}: {
  group: SwarmRunGroup;
  category?: TaskCategory | null;
  onOpen: (group: SwarmRunGroup, detail: SwarmRunDetail | null) => void;
  onOpenUpdate?: (group: SwarmRunGroup, detail: SwarmRunDetail) => void;
  onOpenUnavailable?: (group: SwarmRunGroup) => void;
  onPause?: (group: SwarmRunGroup) => void;
  pausing?: boolean;
}) {
  const live = isLiveSwarmRun(group);
  const { detail, load } = useSwarmRunDetailLoader(group.runId, live);
  const attempts = flattenAttempts(detail?.attempts);
  const sessions = liveSessionCount(attempts);
  const agentLabels = attemptAgentLabels(detail);
  const run = detail?.run;
  const elapsed = elapsedLabel(run?.started_at ?? run?.created_at, run?.finished_at);
  const cost = typeof run?.cost_usd === "number" && run.cost_usd > 0 ? `$${run.cost_usd.toFixed(2)}` : null;
  const pct = group.total > 0 ? Math.round((group.done / group.total) * 100) : 0;
  const boardRunState = TERMINAL_COLUMNS.has(group.column)
    ? null
    : group.root?.run_state ?? group.members.find((task) => task.run_state)?.run_state;
  const statusLabel = run?.status ?? boardRunState ?? (live ? "running" : group.column === "completed" ? "completed" : group.column);
  const staleness = taskStaleness(latestTaskUpdate(group) ?? "");
  const openCard = () => {
    onOpen(group, detail);
    if (detail) {
      return;
    }
    void load().then((next) => {
      if (next) onOpenUpdate?.(group, next);
      else onOpenUnavailable?.(group);
    });
  };

  return (
    <article
      className={`${styles.kcard} ${styles.runCard} ${live ? styles.kcardActive : ""}`}
      role="button"
      tabIndex={0}
      aria-label={`Open swarm run ${group.title}`}
      onClick={openCard}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openCard();
        }
      }}
    >
      <div className={styles.kcardTags}>
        <Badge tone="promote">⣿ Swarm</Badge>
        {category ? (
          <Pill
            tone="neutral"
            title={category.name}
            className={category.color ? styles.categoryPill : undefined}
            style={category.color ? ({ "--cat-color": category.color } as CSSProperties) : undefined}
          >
            {category.name}
          </Pill>
        ) : null}
        <Badge
          className={styles.kcardStatus}
          title={String(statusLabel).replace(/_/g, " ")}
          tone={live ? "running" : group.column === "completed" ? "completed" : group.column === "blocked" ? "danger" : "neutral"}
        >
          {String(statusLabel).replace(/_/g, " ")}
        </Badge>
      </div>
      <h4 className={styles.kcardTitle}>{group.title}</h4>
      <div className={styles.runInsightLine}>
        <span>{sessions > 0 ? `${sessions} session${sessions === 1 ? "" : "s"} live` : `${group.total} task${group.total === 1 ? "" : "s"}`}</span>
        {agentLabels.length ? (
          <span className={styles.agentPills} aria-label="Providers and models">
            {agentLabels.map((label) => <Pill key={label} tone="neutral" className={styles.agentPill} title={label}>{label}</Pill>)}
          </span>
        ) : null}
        {elapsed ? <span>{elapsed}</span> : null}
        {cost ? <span className={styles.runCost}>{cost}</span> : null}
      </div>
      <div className={styles.kcardMeta}>
        <progress className={styles.runProgress} value={pct} max={100} aria-label="Run progress" />
        <span>{group.done}/{group.total}</span>
        {staleness.days >= 1 ? (
          <span className={staleness.stale ? styles.kcardStale : undefined}>
            updated {staleness.days}d ago
          </span>
        ) : null}
        {group.preview.map((line) => (
          <span key={line} className={styles.kcardStep} title={line}>Now: {line}</span>
        ))}
      </div>
      <div className={styles.kcardFoot}>
        {live ? (
          <span className={styles.kcardLive} aria-label="Run in progress">
            <span className={styles.kcardSpinner} aria-hidden="true" />Live
          </span>
        ) : null}
        <div className={styles.kcardActions}>
          <button
            type="button"
            className={styles.kact}
            aria-label={`Expand swarm run ${group.title}`}
            onClick={(event) => { event.preventDefault(); event.stopPropagation(); openCard(); }}
          >
            Expand
          </button>
          {onPause && (live || ["backlog", "ready"].includes(group.column)) ? (
            <button
              type="button"
              className={styles.kact}
              disabled={pausing}
              aria-label={`Pause swarm run ${group.title}`}
              onClick={(event) => { event.preventDefault(); event.stopPropagation(); onPause(group); }}
            >
              {pausing ? "Pausing…" : "Pause"}
            </button>
          ) : null}
        </div>
      </div>
    </article>
  );
}
