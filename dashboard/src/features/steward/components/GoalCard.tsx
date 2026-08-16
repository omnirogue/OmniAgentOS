"use client";

import Link from "next/link";
import { Badge, Card, Sparkline } from "../../../design";
import type { Goal } from "../types";
import styles from "../steward.module.css";

export type GoalCardProps = { goal: Goal };

function formatMetricValue(value: number, unit: string): string {
  if (unit === "usd") return `$${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  if (unit === "ratio") return value.toFixed(2);
  return value.toLocaleString();
}

export function GoalCard({ goal }: GoalCardProps) {
  const metric = goal.north_star.metric;
  const snapshot = metric ? goal.latest[metric] : undefined;
  const series = metric ? (goal.trend[metric] ?? []) : [];
  const points = series.map((row) => row.value);
  const first = points[0];
  const last = points[points.length - 1];
  const deltaPct = first != null && last != null && first !== 0 ? ((last - first) / Math.abs(first)) * 100 : null;

  return (
    <Link href={`/goals/${encodeURIComponent(goal.id)}`} className={styles.goalLink}>
      <Card className={styles.goalCard}>
        <div className={styles.goalHead}>
          <h3 className={styles.goalName}>{goal.name}</h3>
          <Badge tone={goal.status === "active" ? "ok" : "neutral"}>{goal.status}</Badge>
        </div>
        {goal.description ? <p className={styles.goalDescription}>{goal.description}</p> : null}

        {snapshot ? (
          <div className={styles.goalMetricRow}>
            <div className={styles.goalMetricValue}>
              <span className={styles.goalMetricNumber}>{formatMetricValue(snapshot.value, snapshot.unit)}</span>
              <span className={styles.faint}>{metric.replace(/_/g, " ")}</span>
            </div>
            {deltaPct != null ? (
              <Badge tone={deltaPct >= 0 ? "ok" : "danger"}>
                {deltaPct >= 0 ? "+" : ""}
                {deltaPct.toFixed(1)}% / 14d
              </Badge>
            ) : null}
          </div>
        ) : (
          <p className={styles.muted}>No metric snapshots yet.</p>
        )}

        <Sparkline points={points} width={280} height={56} tone={deltaPct != null && deltaPct < 0 ? "danger" : "accent"} aria-label={`${metric} trend`} />
      </Card>
    </Link>
  );
}
