"use client";

import { Badge, Button, Card, EmptyState, Loading, Stat, type BadgeTone } from "@/design";
import { VISION_COLUMNS, type FleetRunView, type SwarmColumnId } from "./columns";
import type { SwarmFleetUtilization, SwarmRunStatus } from "./types";
import styles from "./swarm.module.css";

const COLUMN_LABEL: Record<SwarmColumnId, string> = Object.fromEntries(
  VISION_COLUMNS.map((column) => [column.id, column.label]),
) as Record<SwarmColumnId, string>;

const COLUMN_TONE: Record<SwarmColumnId, string> = Object.fromEntries(
  VISION_COLUMNS.map((column) => [column.id, column.tone]),
) as Record<SwarmColumnId, string>;

function runStatusTone(status: SwarmRunStatus | null): BadgeTone {
  switch (status) {
    case "running":
    case "merging":
      return "running";
    case "planning":
      return "validating";
    case "queued":
      return "queued";
    case "completed":
      return "completed";
    case "failed":
      return "failed";
    case "cancelled":
      return "cancelled";
    default:
      return "neutral";
  }
}

/** Accent for the run card's left rule and progress fill. */
function runTone(run: FleetRunView): string {
  if (run.status === "failed" || run.status === "cancelled") return "var(--danger)";
  if (run.counts.blocked > 0) return "var(--danger)";
  if (run.total > 0 && run.done === run.total) return "var(--ok)";
  if (
    run.counts.running + run.counts.review + run.counts.testing + run.counts.integration >
    0
  ) {
    return "var(--running)";
  }
  return "var(--accent)";
}

function pct(run: FleetRunView): number {
  return run.total > 0 ? Math.round((run.done / run.total) * 100) : 0;
}

export function FleetStrip({
  runs,
  utilization,
  loading,
  onOpenRun,
}: {
  runs: FleetRunView[];
  utilization: SwarmFleetUtilization | null;
  loading: boolean;
  /** Navigate to the combined board filtered to this run (/board?run=). */
  onOpenRun: (runId: string) => void;
}) {
  const hasUtil =
    utilization &&
    Object.values(utilization).some((value) => typeof value === "number");

  if (loading && runs.length === 0) {
    return <Loading variant="skeleton" label="Loading the fleet" lines={3} />;
  }

  if (runs.length === 0) {
    return (
      <EmptyState
        title="No swarm runs yet"
        message="Dispatch one with execute=swarm. Each run fans a brief across up to 10 concurrent provider sessions; its tasks then appear here grouped by run, and on the kanban below. Until then the board carries no swarm cards."
      />
    );
  }

  return (
    <div>
      {hasUtil ? (
        <div className={styles.utilRow}>
          <Stat
            label="Active sessions"
            value={`${utilization?.active_sessions ?? "—"} / ${utilization?.max_sessions_global ?? "—"}`}
            hint="fleet-wide session budget"
          />
          <Stat
            label="Active swarms"
            value={`${utilization?.active_swarms ?? "—"} / ${utilization?.max_concurrent_swarms ?? "—"}`}
            hint="concurrent runs"
          />
          <Stat label="Queued runs" value={utilization?.queued_runs ?? "—"} hint="admission control" />
        </div>
      ) : null}

      <div className={styles.fleetGrid}>
        {runs.map((run) => {
          const tone = runTone(run);
          return (
            <Card
              key={run.id}
              padding="none"
              className={styles.runCard}
              style={{ "--run-tone": tone } as React.CSSProperties}
            >
              <div className={styles.runHead}>
                <span className={styles.runId} title={run.id}>
                  {run.shortId}
                </span>
                {run.status ? (
                  <Badge tone={runStatusTone(run.status)}>{run.status}</Badge>
                ) : (
                  <Badge tone="neutral">{run.total} task{run.total === 1 ? "" : "s"}</Badge>
                )}
              </div>

              {run.goal ? <p className={styles.muted}>{run.goal}</p> : null}

              <div className={styles.chips}>
                {VISION_COLUMNS.map((column) =>
                  run.counts[column.id] > 0 ? (
                    <Badge key={column.id} tone="neutral">
                      <span
                        aria-hidden="true"
                        style={{
                          display: "inline-block",
                          width: "0.5rem",
                          height: "0.5rem",
                          borderRadius: "999px",
                          background: COLUMN_TONE[column.id],
                          marginRight: "0.35rem",
                        }}
                      />
                      {COLUMN_LABEL[column.id]}: {run.counts[column.id]}
                    </Badge>
                  ) : null,
                )}
              </div>

              <div className={styles.progress}>
                <div
                  className={styles.progressTrack}
                  role="progressbar"
                  aria-label="Run progress"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={pct(run)}
                >
                  <div className={styles.progressFill} style={{ width: `${pct(run)}%` }} />
                </div>
                <span className={styles.progressLabel}>
                  {run.done}/{run.total}
                </span>
              </div>

              <div className={styles.cardFooter}>
                <span className={styles.muted}>
                  {run.costUsd != null
                    ? `$${run.costUsd.toFixed(2)}${run.budgetUsdMax != null ? ` / $${run.budgetUsdMax.toFixed(0)}` : ""}`
                    : ""}
                </span>
                <Button variant="ghost" size="sm" onClick={() => onOpenRun(run.id)}>
                  Open on board
                </Button>
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
