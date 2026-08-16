"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { startVisibilityPoll } from "../lib/pollWhenVisible";
import { useEventChannel } from "../lib/useEventChannel";
import { cx } from "./utils";

/**
 * Topbar system pill — the prototype's `.sys-status`: a heartbeat dot, the run state
 * (Running / Paused), API health, and how fresh this readout itself is.
 *
 * Reads GET /api/health and GET /api/pause through the same-origin proxy (API_BASE is
 * ""), polls only while the tab is visible, and takes the instant `pause.changed`
 * event off the shell's single shared EventSource so a pause shows up immediately
 * instead of up to a poll late.
 *
 * Read-only by design: this lane ships no pause/resume mutation (that UX lands later),
 * so the pill states what is true and never pretends to change it.
 */

export type StatusPillTone = "ok" | "warn" | "danger" | "neutral";

export type StatusPillView = {
  tone: StatusPillTone;
  /** Primary word: the run state. */
  state: string;
  /** Secondary clause: what the API/worker is doing. */
  detail: string;
  /** True while the system is deliberately paused (amber, not red). */
  paused: boolean;
};

type Inputs = {
  /** null => the health read failed or has not happened yet. */
  health: { ok: boolean; workerAlive: boolean } | null;
  paused: boolean | null;
  loading: boolean;
};

/**
 * Pure state machine, exported for tests. Precedence is deliberate:
 *   1. we cannot reach the API      → danger  (nothing else we say is trustworthy)
 *   2. the operator paused the loop → amber   (a hold is caution, not failure)
 *   3. the worker is not beating    → amber   (API answers, machine does not run)
 *   4. otherwise                    → ok
 */
export function statusPillView({ health, paused, loading }: Inputs): StatusPillView {
  if (!health) {
    return loading
      ? { tone: "neutral", state: "Checking", detail: "contacting API", paused: false }
      : { tone: "danger", state: "Offline", detail: "API unreachable", paused: false };
  }
  if (!health.ok) {
    return { tone: "danger", state: "Offline", detail: "API degraded", paused: false };
  }
  if (paused) {
    return { tone: "warn", state: "Paused", detail: "API OK", paused: true };
  }
  if (!health.workerAlive) {
    return { tone: "warn", state: "Running", detail: "worker stale", paused: false };
  }
  return { tone: "ok", state: "Running", detail: "API OK", paused: false };
}

/** "just now" / "4m ago" — coarse on purpose; this is a freshness cue, not a clock. */
export function relativeSince(from: number | null, now: number): string {
  if (from === null) return "never synced";
  const sec = Math.max(0, Math.round((now - from) / 1000));
  if (sec < 10) return "synced just now";
  if (sec < 60) return `synced ${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `synced ${min}m ago`;
  return `synced ${Math.floor(min / 60)}h ago`;
}

const POLL_MS = 15_000;
/** Re-renders the freshness clause even when polling is stalled (hidden tab), so the
 * pill can admit it is stale instead of freezing on "synced just now". */
const TICK_MS = 20_000;

export function StatusPill() {
  const [health, setHealth] = useState<Inputs["health"]>(null);
  const [paused, setPaused] = useState<boolean | null>(null);
  const [pauseReason, setPauseReason] = useState("");
  const [loading, setLoading] = useState(true);
  const [syncedAt, setSyncedAt] = useState<number | null>(null);
  const [lastBeat, setLastBeat] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const { lastEvent } = useEventChannel(["pause.changed"]);

  const refresh = useCallback(async () => {
    // Independent reads: a pause-endpoint hiccup must not blank out health, and vice
    // versa. Only a health answer counts as "synced" — it is the trust anchor.
    const [healthResult, pauseResult] = await Promise.allSettled([api.health(), api.pause()]);
    if (healthResult.status === "fulfilled") {
      const value = healthResult.value;
      setHealth({ ok: value.status === "ok" && Boolean(value.db), workerAlive: Boolean(value.worker?.alive) });
      setLastBeat(value.worker?.last_beat_at ?? null);
      setSyncedAt(Date.now());
    } else {
      setHealth(null);
    }
    if (pauseResult.status === "fulfilled") {
      setPaused(Boolean(pauseResult.value.paused));
      setPauseReason(pauseResult.value.reason ?? "");
    }
    setNow(Date.now());
    setLoading(false);
  }, []);

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), POLL_MS);
  }, [refresh]);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    const payload = lastEvent?.payload ?? {};
    if (lastEvent?.type === "pause.changed" && typeof payload.paused === "boolean") {
      setPaused(payload.paused);
      setPauseReason(typeof payload.reason === "string" ? payload.reason : "");
    }
  }, [lastEvent]);

  const view = statusPillView({ health, paused, loading });
  const freshness = relativeSince(syncedAt, now);
  const title = [
    view.paused && pauseReason ? `Paused — ${pauseReason}` : view.state,
    view.detail,
    lastBeat ? `worker beat ${new Date(lastBeat).toLocaleTimeString()}` : null,
    freshness,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div
      className={cx("ds-status-pill", `ds-status-pill--${view.tone}`)}
      role="status"
      aria-live="polite"
      aria-label={`System ${view.state}, ${view.detail}, ${freshness}`}
      title={title}
    >
      <span
        className={cx("ds-status-pill__dot", view.tone === "ok" && !view.paused && "ds-status-pill__dot--live")}
        aria-hidden="true"
      />
      <span className="ds-status-pill__state">{view.state}</span>
      <span className="ds-status-pill__sep" aria-hidden="true" />
      <span className="ds-status-pill__detail">{view.detail}</span>
      <span className="ds-status-pill__sep ds-status-pill__sep--wide" aria-hidden="true" />
      <span className="ds-status-pill__detail ds-status-pill__detail--wide">{freshness}</span>
    </div>
  );
}
