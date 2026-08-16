"use client";

/**
 * RunsTab (§2.6 Runs tab) — attempt timeline + steering log + sub-task
 * disclosure. ApprovalsTab — the pending approval with the REAL command and
 * risk, decidable inline.
 */

import { useState } from "react";
import Link from "next/link";
import { Badge, Button, Card, EmptyState } from "@/design";
import { api } from "@/lib/api";
import type {
  LiveBoardTask,
  LonghaulTaskDetail,
  TaskTurn,
} from "@/features/collab/types";
import { AttemptTimeline } from "./AttemptTimeline";
import styles from "./board.module.css";

// ── Runs ──────────────────────────────────────────────────────

export function RunsTab({
  longhaul,
  conversation,
  subTasks,
}: {
  longhaul: LonghaulTaskDetail | null;
  conversation: TaskTurn[];
  subTasks: LiveBoardTask[];
}) {
  const attempts = longhaul?.attempts ?? [];
  return (
    <div className={styles.runsGrid}>
      <section>
        <h4 className={styles.sectionTitle}>Attempts</h4>
        <AttemptTimeline attempts={attempts} />
      </section>

      <section>
        <h4 className={styles.sectionTitle}>Steering log</h4>
        {conversation.length ? (
          <div className={styles.steeringList}>
            {conversation.map((turn) => (
              <div key={turn.id} className={styles.steeringRow}>
                <Badge tone="neutral">{turn.role}</Badge>
                <span className={styles.steeringContent}>{turn.content}</span>
                <span className={styles.muted}>
                  {turn.delivery?.pending
                    ? "pending"
                    : turn.delivery?.delivered_at
                      ? "delivered"
                      : ""}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState message="No steering messages were sent to this task." />
        )}
      </section>

      <section>
        <h4 className={styles.sectionTitle}>Sub-tasks</h4>
        {subTasks.length ? (
          <div className={styles.childList}>
            {subTasks.map((child) => (
              <Link
                key={child.id}
                href={`/board?task=${encodeURIComponent(child.id)}`}
                className={styles.childRow}
              >
                <Badge tone={child.status === "done" ? "completed" : "neutral"}>
                  {child.status.replace(/_/g, " ")}
                </Badge>
                <span className={styles.childTitle}>{child.title}</span>
              </Link>
            ))}
          </div>
        ) : (
          <EmptyState message="No sub-tasks — fan out from a chat to create them." />
        )}
      </section>
    </div>
  );
}

// ── Approvals ─────────────────────────────────────────────────

export function ApprovalsTab({
  task,
  onDecided,
}: {
  task: LiveBoardTask;
  onDecided: () => void;
}) {
  const approval = task.pending_approval ?? null;
  const [busy, setBusy] = useState<"approved" | "rejected" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  if (!approval) {
    return (
      <EmptyState
        title="Nothing waiting on you"
        message="When this task's session needs a risky command approved, it lands here with the real command."
      />
    );
  }

  const decide = async (decision: "approved" | "rejected") => {
    setBusy(decision);
    setNotice(null);
    try {
      await api.decideApproval(approval.id, decision);
      setNotice(decision === "approved" ? "Approved — the session resumes on its next poll." : "Rejected.");
      onDecided();
    } catch (reason) {
      setNotice(reason instanceof Error ? `${decision} failed: ${reason.message}` : `${decision} failed.`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Card padding="sm" className={styles.approvalCard}>
      <div className={styles.approvalHead}>
        <Badge tone="warn">Waiting on you</Badge>
        {approval.action_class ? (
          <Badge tone="neutral">{approval.action_class.replace(/_/g, " ")}</Badge>
        ) : null}
        {approval.risk ? <Badge tone="danger">{approval.risk}</Badge> : null}
      </div>
      <code className={styles.approvalCommand}>{approval.command}</code>
      <div className={styles.approvalActions}>
        <Button
          variant="primary"
          size="sm"
          disabled={busy !== null}
          onClick={() => void decide("approved")}
        >
          {busy === "approved" ? "Approving…" : "Approve"}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={busy !== null}
          onClick={() => void decide("rejected")}
        >
          {busy === "rejected" ? "Rejecting…" : "Reject"}
        </Button>
      </div>
      {notice ? <p className={styles.muted} role="status">{notice}</p> : null}
    </Card>
  );
}
