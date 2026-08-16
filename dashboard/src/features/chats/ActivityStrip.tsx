"use client";

/**
 * Compact activity strip inside the streaming bubble.
 *
 * Replaces the bare blinking caret: while a turn runs you get elapsed time,
 * the last SSE event the client actually received, and — when the bridge has
 * gone quiet past the 6s stall threshold — an explicit "no live output"
 * indicator that names the poll fallback carrying the reply.
 *
 * Only the stalled note is announced (role="status"); the ticking clock is
 * deliberately NOT live, because a per-second announcement is unusable.
 */

import { useEffect, useState } from "react";
import type { TurnEventKind, TurnState } from "./useChats";
import styles from "./chatShell.module.css";

export interface ActivityStripProps {
  /** Epoch ms the turn started; null before the first send resolves. */
  startedAt: number | null;
  /** Last SSE frame kind the reducer saw (null = nothing yet). */
  lastEventType: TurnEventKind | null;
  state: TurnState;
  /** Injectable clock so tests don't need timers. */
  now?: number;
}

const EVENT_LABEL: Record<TurnEventKind, string> = {
  started: "turn started",
  delta: "streaming",
  poll: "polled the transcript",
  completed: "completed",
};

export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

export function ActivityStrip({ startedAt, lastEventType, state, now }: ActivityStripProps) {
  const [tick, setTick] = useState(() => now ?? Date.now());

  useEffect(() => {
    if (now !== undefined) return;
    setTick(Date.now());
    const timer = setInterval(() => setTick(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [now, startedAt]);

  const current = now ?? tick;
  const stalled = state === "stalled";
  const elapsed = startedAt === null ? null : formatElapsed(current - startedAt);

  return (
    <div
      className={`${styles.activityStrip}${stalled ? ` ${styles.activityStalled}` : ""}`}
      aria-label="Turn activity"
    >
      <span className={styles.activityDot} aria-hidden="true" />
      {elapsed ? <span>{elapsed}</span> : null}
      {elapsed ? (
        <span className={styles.activitySep} aria-hidden="true">
          ·
        </span>
      ) : null}
      <span>
        {lastEventType
          ? EVENT_LABEL[lastEventType]
          : state === "queued"
            ? "queued"
            : "waiting for the first event"}
      </span>
      {stalled ? (
        <>
          <span className={styles.activitySep} aria-hidden="true">
            ·
          </span>
          <span className={styles.activityStalledNote} role="status">
            no live output — polling for the reply
          </span>
        </>
      ) : null}
    </div>
  );
}
