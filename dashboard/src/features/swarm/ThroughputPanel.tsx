"use client";

import { EmptyState, Loading, Sparkline, Stat, type StatTone } from "@/design";
import type { SwarmOverview } from "./types";
import styles from "./swarm.module.css";

/** Coarse fleet-health string → Stat tone. */
function healthTone(health: string): StatTone {
  switch (health) {
    case "healthy":
      return "ok";
    case "degraded":
      return "warn";
    case "stalled":
    case "failed":
      return "danger";
    default:
      return "default";
  }
}

/** Minutes → a compact "1.5h" / "42m" label, sign-preserving. */
function fmtMinutes(minutes: number): string {
  const sign = minutes < 0 ? "-" : "";
  const abs = Math.abs(minutes);
  if (abs >= 60) return `${sign}${(abs / 60).toFixed(1)}h`;
  return `${sign}${Math.round(abs)}m`;
}

/**
 * Live throughput tiles against `GET /api/swarm/overview` (C2). Speedup vs the
 * frozen manual estimate, terminal utilization, tasks/hour (with a sparkline
 * accumulated across polls), rate-limit delays avoided, and a coarse health
 * string. Renders a graceful EmptyState when no run is active — the same calm,
 * permanent condition the fleet strip shows.
 *
 * The estimates themselves are WP7's (`swarm/summary.py`) to freeze; until it
 * lands the overview route computes them from `metrics_json`/`plan_json`
 * directly, so these tiles are already live end-to-end.
 */
export function ThroughputPanel({
  overview,
  history,
  loading,
  hasLoaded,
  stale,
}: {
  overview: SwarmOverview | null;
  history: number[];
  loading: boolean;
  hasLoaded: boolean;
  stale: boolean;
}) {
  if (loading && !hasLoaded) {
    return <Loading variant="skeleton" label="Loading swarm metrics" lines={2} />;
  }

  if (!overview || overview.active === 0) {
    return (
      <EmptyState
        title="No active swarm runs"
        message="Throughput tiles — speedup, utilization, tasks/hour, delays avoided — light up while runs are executing. Dispatch one with execute=swarm to populate them."
      />
    );
  }

  const t = overview.throughput;

  return (
    <div>
      {stale ? (
        <div className={styles.banner} role="status">
          Metrics may be stale — the last poll failed
        </div>
      ) : null}

      <div className={styles.utilRow}>
        <Stat
          label="Speedup"
          value={`${t.speedup.toFixed(1)}×`}
          tone={t.speedup >= 1 ? "ok" : "default"}
          hint="Σ est manual ÷ wall"
        />
        <Stat
          label="Time saved"
          value={fmtMinutes(t.time_saved_minutes)}
          tone={t.time_saved_minutes > 0 ? "ok" : "default"}
          hint="vs manual estimate"
        />
        <Stat
          label="Utilization"
          value={`${t.utilization_pct.toFixed(0)}%`}
          hint={`${t.active_terminals} / ${t.max_terminals} terminals`}
        />
        <Stat
          label="Tasks / hour"
          value={t.tasks_per_hour.toFixed(1)}
          hint={`${overview.progress.done} / ${overview.progress.total} done`}
        />
        <Stat
          label="Delays avoided"
          value={t.rate_limit_delays_avoided}
          hint="provider reroutes"
        />
        <Stat
          label="Fleet health"
          value={t.health}
          tone={healthTone(t.health)}
          hint="across active runs"
        />
      </div>

      {/* Tasks/hour is a point-in-time value; the hook accumulates a bounded
          in-memory series across 15s polls so the trend is visible without any
          server-side history table. Sparkline renders an em dash until it has
          ≥2 samples. */}
      <div className={styles.sparkRow}>
        <span className={styles.muted}>Tasks/hour trend</span>
        <Sparkline
          points={history}
          tone="accent"
          width={160}
          height={28}
          aria-label="Tasks per hour over recent polls"
        />
      </div>
    </div>
  );
}
