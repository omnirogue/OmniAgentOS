"use client";

/**
 * AttemptTimeline (§2.6 Runs tab) — extracted from the /activity page's
 * inline timeline so the drawer and the full-page detail render the SAME
 * component. One card per attempt: seq, harness, model, duration, end reason.
 */

import { Badge, Card, DefinitionList, EmptyState } from "@/design";
import type { TaskAttempt } from "@/features/collab/types";
import { attemptOutcome } from "./attemptOutcome";
import styles from "./board.module.css";

function formatTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function duration(start: string, end: string | null): string {
  const ms = Date.parse(end ?? new Date().toISOString()) - Date.parse(start);
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const seconds = Math.round(ms / 1000);
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

export function AttemptTimeline({ attempts }: { attempts: TaskAttempt[] }) {
  if (!attempts.length) {
    return <EmptyState title="Not attempted yet" message="No session attempts were recorded for this task." />;
  }
  return (
    <div className={styles.attemptList}>
      {attempts.map((attempt) => {
        const outcome = attemptOutcome(attempt.end_reason, attempt.ended_at === null);
        return (
          <Card key={attempt.id} padding="sm" className={styles.attemptCard}>
            <div className={styles.attemptHead}>
              <strong>Attempt #{attempt.seq}</strong>
              <Badge tone={outcome.tone} category="result">
                {outcome.label}
              </Badge>
            </div>
            <DefinitionList
              items={[
                { label: "Harness", value: attempt.harness },
                { label: "Model", value: attempt.model },
                ...(attempt.tier ? [{ label: "Tier", value: attempt.tier }] : []),
                { label: "Account", value: attempt.account_id ?? "—" },
                { label: "Started", value: formatTime(attempt.started_at) },
                { label: "Duration", value: duration(attempt.started_at, attempt.ended_at) },
                { label: "End reason", value: attempt.end_reason ?? "unknown" },
                ...(attempt.detail ? [{ label: "Verdict", value: attempt.detail }] : []),
              ]}
            />
          </Card>
        );
      })}
    </div>
  );
}
