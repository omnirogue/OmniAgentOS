"use client";

import Link from "next/link";
import {
  Badge,
  EmptyState,
  ErrorState,
  Icon,
  Loading,
  StatusDot,
} from "@/design";
import { useActiveRoutines } from "@/features/pulse/hooks";
import type { AsyncStatus } from "@/features/pulse/hooks";
import styles from "@/features/pulse/pulse.module.css";

/**
 * Cockpit Loops pulse — 3–5 active routines with last-run status and next
 * fire. A compact list at the bottom of the cockpit surface so the
 * operator sees "what ran, what's coming up" at a glance without leaving
 * the front door.
 *
 * Data path: ``fetchActiveRoutines`` (client.ts) joins the live
 * ``/api/routines?status=active`` roster with S5's ``/api/routines/runs``
 * aggregate (newest run per routine); next-fire countdowns come from the
 * same cron parser the Loops page uses. A failure renders the tile's
 * ErrorState with Retry — never fixture data presented as live.
 */
export function LoopsPulse() {
  const { rows, status, error, refresh } = useActiveRoutines(5);

  return (
    <section className={styles.cockpitPulse} aria-label="Loops">
      <div className={styles.cockpitPulseHead}>
        <div>
          <h2 className={styles.cockpitPulseTitle}>Loops</h2>
          <p className={styles.cockpitPulseSub}>
            Active routines and their last-run status.
          </p>
        </div>
        <Link
          href="/loops"
          className="ds-btn ds-btn--ghost ds-btn--sm"
          aria-label="Open all loops"
        >
          All loops
          <Icon name="chevronRight" size={12} />
        </Link>
      </div>
      <LoopsBody status={status} error={error} rows={rows} onRetry={refresh} />
    </section>
  );
}

function LoopsBody({
  status,
  error,
  rows,
  onRetry,
}: {
  status: AsyncStatus;
  error: string | null;
  rows: ReturnType<typeof useActiveRoutines>["rows"];
  onRetry: () => void;
}) {
  if (status === "loading") {
    return (
      <div aria-busy="true">
        <Loading />
      </div>
    );
  }
  if (status === "error") {
    return (
      <ErrorState
        message={error ?? "Could not load routine runs"}
        retryLabel="Retry"
        onRetry={onRetry}
      />
    );
  }
  if (status === "empty" || rows.length === 0) {
    return (
      <EmptyState
        icon={<Icon name="clock" size={20} />}
        message="No active routines yet. Create a first loop from /loops."
      />
    );
  }
  return (
    <ul className={styles.loopList}>
      {rows.map((row) => {
        const statusTone = runStatusTone(row.lastRunStatus);
        return (
          <li key={row.id} className={styles.loopRow}>
            <StatusDot state={statusTone} />
            <span className={styles.loopName} title={row.name}>
              {row.name}
            </span>
            <span className={styles.loopMeta}>
              {row.acceptanceRate === null
                ? "—"
                : `${(row.acceptanceRate * 100).toFixed(0)}%`}
            </span>
            <span className={styles.loopNextFire}>
              {formatNextFire(row.nextFire)}
            </span>
            <Badge tone={badgeTone(row.lastRunStatus)}>
              {row.lastRunStatus}
            </Badge>
          </li>
        );
      })}
    </ul>
  );
}

function runStatusTone(
  lastRunStatus: string,
): "completed" | "failed" | "running" | "queued" {
  switch (lastRunStatus) {
    case "passed":
    case "accepted":
    case "confirmed":
      return "completed";
    case "failed":
    case "rejected":
    case "error":
      return "failed";
    case "running":
    case "in_progress":
      return "running";
    default:
      return "queued";
  }
}

function badgeTone(lastRunStatus: string): "ok" | "danger" | "running" | "neutral" {
  switch (lastRunStatus) {
    case "passed":
    case "accepted":
    case "confirmed":
      return "ok";
    case "failed":
    case "rejected":
    case "error":
      return "danger";
    case "running":
    case "in_progress":
      return "running";
    default:
      return "neutral";
  }
}

function formatNextFire(value: string): string {
  if (!value || value === "—") return "—";
  // Already a human string like "in 4h 12m" → pass through.
  if (value.startsWith("in ")) return value;
  // ISO timestamp → best-effort relative countdown.
  const when = Date.parse(value);
  if (Number.isNaN(when)) return value;
  const diffMs = when - Date.now();
  if (diffMs <= 0) return "now";
  const totalMinutes = Math.ceil(diffMs / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours === 0) return `in ${minutes}m`;
  if (minutes === 0) return `in ${hours}h`;
  return `in ${hours}h ${minutes}m`;
}
