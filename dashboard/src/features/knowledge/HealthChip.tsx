"use client";

import { Badge, StatusDot, Tooltip } from "../../design";
import type { KnowledgeStats } from "./types";

export type RecallHealthState = "ok" | "warn" | "danger" | "idle";

export function recallHealthState(stats: KnowledgeStats, now = Date.now()): RecallHealthState {
  // If the KB is not available, it's in danger state
  if (!stats.available) return "danger";

  // If there are no recalls yet but the KB is available, it's idle/new
  if (stats.recalls.count === 0) return "idle";

  const last = stats.health.last_successful_recall_at ? Date.parse(stats.health.last_successful_recall_at) : Number.NaN;

  // If we don't have a last successful recall timestamp, or it's older than 1 hour, it's stale/danger
  if (!Number.isFinite(last) || now - last > 60 * 60 * 1000) return "danger";

  // If there are errors in the last 24 hours, it's a warning
  return stats.health.recall_errors_24h > 0 ? "warn" : "ok";
}

const LABELS: Record<RecallHealthState, string> = {
  ok: "Recall healthy",
  warn: "Recall has warnings",
  danger: "Recall unavailable",
  idle: "No recalls yet",
};

export function HealthChip({ stats }: { stats: KnowledgeStats }) {
  const state = recallHealthState(stats);
  const last = stats.health.last_successful_recall_at ? new Date(stats.health.last_successful_recall_at).toLocaleString() : "No successful recall recorded";
  return (
    <Tooltip
      content={
        <span>
          <strong>{LABELS[state]}</strong>
          <br />
          Last success: {last}
          <br />
          Errors (24h): {stats.health.recall_errors_24h}
          {stats.health.last_error ? (
            <>
              <br />
              Last error: {stats.health.last_error}
            </>
          ) : null}
        </span>
      }
      side="bottom"
    >
      <Badge
        tone={state === "idle" ? "neutral" : state}
        title={LABELS[state]}
        style={{ display: "inline-flex", alignItems: "center", gap: "var(--space-2)", cursor: "help" }}
      >
        <StatusDot state={state === "idle" ? "ok" : state} label={LABELS[state]} />
        {LABELS[state]}
      </Badge>
    </Tooltip>
  );
}
