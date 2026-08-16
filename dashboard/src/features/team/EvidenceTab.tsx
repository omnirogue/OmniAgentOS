"use client";

/**
 * EvidenceTab (P5) — TaskDetailPanel's new tab: this card's attributed
 * artifacts (`GET /api/team/tasks/{id}/evidence`), grouped by kind, each row
 * an attribution badge (deterministic/manual) + a quality-gate badge (pass
 * vs rejected/reverted/excessive_attempts) + "mark mis-attributed"
 * (`PATCH .../evidence/{id}` with `task_id: null`). The card's append-only
 * audit trail (`GET /api/team/tasks/{id}/events`) renders as a History list
 * at the bottom — same tab, least churn to the existing six-tab layout.
 */

import { useCallback, useEffect, useState } from "react";
import { Badge, Button, EmptyState, ErrorState, Loading } from "@/design";
import type { BadgeTone } from "@/design";
import { teamApi, TeamApiError } from "./client";
import { EVIDENCE_GROUPS } from "./types";
import type { TeamEvent, TeamEvidence } from "./types";
import styles from "./team.module.css";

const KIND_ICON: Record<string, string> = {
  commit: "◆",
  pr: "⇄",
  review: "☑",
  session: "▶",
  test_run: "✓",
  deploy: "▲",
  doc: "▤",
  customer_reply: "✉",
  research: "◎",
  note: "✎",
};

function qualityTone(gate: string): BadgeTone {
  return gate === "pass" ? "ok" : "danger";
}

function attributionTone(attribution: string): BadgeTone {
  return attribution === "manual" ? "promote" : "neutral";
}

function EvidenceRow({ evidence, onMarkMisattributed, busy }: {
  evidence: TeamEvidence;
  onMarkMisattributed: (evidenceId: string) => void;
  busy: boolean;
}) {
  return (
    <div className={styles.evidenceRow}>
      <span className={styles.evidenceIcon} aria-hidden="true">{KIND_ICON[evidence.kind] ?? "•"}</span>
      <span className={styles.evidenceMain} title={evidence.title || evidence.ref}>
        {evidence.title || evidence.ref}
        <span className={styles.evidenceRef}>{evidence.ref}</span>
      </span>
      <Badge tone={attributionTone(evidence.attribution)}>{evidence.attribution}</Badge>
      <Badge tone={qualityTone(evidence.quality_gate)}>{evidence.quality_gate.replace(/_/g, " ")}</Badge>
      <Button
        variant="ghost"
        size="sm"
        disabled={busy}
        onClick={() => onMarkMisattributed(evidence.id)}
        title="Detach this artifact from this card"
      >
        {busy ? "Marking…" : "Mark mis-attributed"}
      </Button>
    </div>
  );
}

export function EvidenceTab({ taskId }: { taskId: string }) {
  const [evidence, setEvidence] = useState<TeamEvidence[]>([]);
  const [events, setEvents] = useState<TeamEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [evidenceRows, eventRows] = await Promise.all([
        teamApi.evidence(taskId),
        teamApi.events(taskId),
      ]);
      setEvidence(evidenceRows);
      setEvents(eventRows);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load evidence");
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => { void refresh(); }, [refresh]);

  const markMisattributed = async (evidenceId: string) => {
    setBusyId(evidenceId);
    setActionError(null);
    try {
      await teamApi.reattributeEvidence(evidenceId, null, "operator");
      await refresh();
    } catch (reason) {
      setActionError(
        reason instanceof TeamApiError || reason instanceof Error
          ? reason.message
          : "Reattribution failed.",
      );
    } finally {
      setBusyId(null);
    }
  };

  if (loading && evidence.length === 0 && events.length === 0) {
    return <Loading variant="skeleton" label="Loading evidence" lines={4} />;
  }
  if (error) {
    return <ErrorState message={error} onRetry={() => void refresh()} />;
  }

  const groups = EVIDENCE_GROUPS.map((group) => ({
    ...group,
    rows: evidence.filter((row) => (group.kinds as readonly string[]).includes(row.kind)),
  })).filter((group) => group.rows.length > 0);
  const knownKinds = new Set(EVIDENCE_GROUPS.flatMap((group) => group.kinds));
  const uncategorized = evidence.filter((row) => !knownKinds.has(row.kind));

  return (
    <div>
      {actionError ? <p className={styles.verifyError}>{actionError}</p> : null}
      {evidence.length === 0 ? (
        <EmptyState title="No evidence yet" message="Nothing has been attributed to this card." />
      ) : (
        <>
          {groups.map((group) => (
            <div key={group.id} className={styles.evidenceGroup}>
              <h4 className={styles.evidenceGroupTitle}>{group.label}</h4>
              {group.rows.map((row) => (
                <EvidenceRow key={row.id} evidence={row} onMarkMisattributed={(id) => void markMisattributed(id)} busy={busyId === row.id} />
              ))}
            </div>
          ))}
          {uncategorized.length > 0 ? (
            <div className={styles.evidenceGroup}>
              <h4 className={styles.evidenceGroupTitle}>Other</h4>
              {uncategorized.map((row) => (
                <EvidenceRow key={row.id} evidence={row} onMarkMisattributed={(id) => void markMisattributed(id)} busy={busyId === row.id} />
              ))}
            </div>
          ) : null}
        </>
      )}

      <div className={styles.eventsList} aria-label="Card history">
        <h4 className={styles.evidenceGroupTitle}>History</h4>
        {events.length === 0 ? (
          <p className={styles.bucketEmpty}>No recorded events.</p>
        ) : (
          events.map((event) => (
            <div key={event.id} className={styles.eventRow}>
              <span className={styles.eventAt}>{event.created_at}</span>
              <span>
                {event.actor} · {event.event.replace(/_/g, " ")}
                {event.from_status && event.to_status ? ` (${event.from_status} → ${event.to_status})` : ""}
                {event.note ? ` — ${event.note}` : ""}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
