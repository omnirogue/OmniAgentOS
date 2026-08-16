"use client";

import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  Icon,
  Loading,
} from "@/design";
import styles from "./routines.module.css";
import type { RecentRunItem } from "./types";

/** How long ago relative to now. Renders minutes/hours/days, with a short
 * "just now" floor under a minute, matching the rest of the dashboard's
 * "recent activity" voice. */
function relativeTime(iso: string | null): string {
  if (!iso) return "unknown";
  const when = Date.parse(iso);
  if (Number.isNaN(when)) return iso;
  const diffMs = Date.now() - when;
  if (diffMs < 0) return "just now";
  const totalSec = Math.floor(diffMs / 1000);
  if (totalSec < 60) return "just now";
  const minutes = Math.floor(totalSec / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function runOutcome(run: RecentRunItem) {
  if (run.accepted === true) return { label: "Accepted", tone: "ok" as const };
  if (run.accepted === false) return { label: "Rejected", tone: "danger" as const };
  if (run.gate_passed === true) return { label: "Gate passed", tone: "ok" as const };
  if (run.gate_passed === false) return { label: "Gate failed", tone: "danger" as const };
  return { label: "Recorded", tone: "neutral" as const };
}

// ISSUE-8 (Sol review, seam 2): `cost_usd` is `null` when this run's true
// cost was never reported (omniagentos/scheduler/store.py, nullable since
// migration 120) — genuinely unknown, never a manufactured "$0.00". Same
// convention as features/routines/format.ts's costPerAcceptedChangeLabel and
// features/loop-health/loopHealth.ts's totalCostText: say "unknown".
function costLabelFor(run: RecentRunItem): string {
  if (run.cost_usd === null || run.cost_usd === undefined) return "unknown";
  return `$${run.cost_usd.toFixed(2)}`;
}

function RunRow({ run }: { run: RecentRunItem }) {
  const outcome = runOutcome(run);
  const costLabel = costLabelFor(run);
  return (
    <li className={styles.timelineRow}>
      <span className={styles.timelineDot} data-tone={outcome.tone} aria-hidden />
      <div className={styles.timelineBody}>
        <div className={styles.timelineHeader}>
          <span className={styles.timelineName}>{run.routine_name}</span>
          <Badge tone={outcome.tone}>{outcome.label}</Badge>
        </div>
        <div className={styles.timelineMeta}>
          {run.run_id ? (
            <span className={styles.mono}>{run.run_id}</span>
          ) : null}
          <span>·</span>
          <span>{costLabel}</span>
          <span>·</span>
          <time dateTime={run.finished_at ?? ""} title={run.finished_at ?? undefined}>
            {relativeTime(run.finished_at)}
          </time>
        </div>
      </div>
    </li>
  );
}

/** Timeline of what the system did on its own — routine name, run id, gate
 * passed, accepted, cost, finished_at. Real components, no raw JSON.
 *
 * Presentational: the Loops page owns the single ``useRecentRuns`` call (its
 * table sparklines read the same aggregate) and passes the result down — one
 * fetch per mount, never two. */
export function RecentRunsPanel({
  runs,
  loading,
  error,
  refresh,
}: {
  runs: RecentRunItem[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}) {
  return (
    <Card raised>
      <div className={styles.panelHeader}>
        <div>
          <h3 className={styles.panelTitle}>Recent runs</h3>
          <p className={styles.panelSubtitle}>
            What the system has done on its own — newest first.
          </p>
        </div>
        <Icon name="clock" size={14} className={styles.refreshIcon} aria-hidden />
      </div>

      {loading ? <Loading variant="skeleton" label="Loading recent runs…" lines={4} /> : null}
      {!loading && error ? <ErrorState message={error} onRetry={refresh} /> : null}
      {!loading && !error && runs.length === 0 ? (
        <EmptyState
          title="No runs yet"
          message="When a loop fires, its gate result, acceptance, cost, and finished time land here."
        />
      ) : null}
      {!loading && !error && runs.length > 0 ? (
        <ul className={styles.timeline} aria-label="Recent loop runs">
          {runs.map((run, idx) => (
            <RunRow key={`${run.routine_id}-${run.run_id ?? run.finished_at ?? idx}-${idx}`} run={run} />
          ))}
        </ul>
      ) : null}
    </Card>
  );
}
