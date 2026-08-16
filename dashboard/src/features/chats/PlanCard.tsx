"use client";

/**
 * PlanCard (chat-v2 §2.6) — the in-thread plan preview from a plan job.
 * Approve & run confirms (POST /api/intake/plan/{job_id}/confirm); Discard
 * clears. Renders the planner's decomposition as components — never a JSON
 * dump.
 */

import { useState } from "react";
import { Badge, Button, ErrorState, Loading, Select } from "@/design";
import type { PlanJobStatus } from "./chatApi";
import styles from "./chats.module.css";

interface PlanCardProps {
  job: PlanJobStatus | null;
  busy: boolean;
  error: string | null;
  projectOptions: Array<{ value: string; label: string }>;
  onConfirm: (projectOverride?: string) => Promise<void>;
  onDiscard: () => void;
}

export function PlanCard({
  job,
  busy,
  error,
  projectOptions,
  onConfirm,
  onDiscard,
}: PlanCardProps) {
  const [override, setOverride] = useState("auto");
  const [confirming, setConfirming] = useState(false);

  if (busy && !job) {
    return (
      <div className={styles.planCard}>
        <Loading variant="skeleton" label="Planning" lines={3} />
      </div>
    );
  }
  if (error) {
    return (
      <div className={styles.planCard}>
        <ErrorState title="Planning failed" message={error} onRetry={onDiscard} />
      </div>
    );
  }
  if (!job) return null;

  if (job.status === "running") {
    return (
      <div className={styles.planCard}>
        <div className={styles.planCardHead}>
          <Badge tone="running">planning…</Badge>
          <span className={styles.planCardTitle}>Fable is decomposing the goal</span>
        </div>
        <Loading variant="skeleton" label="Plan job running" lines={2} />
      </div>
    );
  }

  if (job.status === "error") {
    return (
      <div className={styles.planCard}>
        <ErrorState
          title="The plan job failed"
          message={job.error ?? "Unknown planning error"}
          onRetry={onDiscard}
        />
      </div>
    );
  }

  const plan = job.plan;
  const tasks = plan?.tasks ?? [];
  const subProjects = plan?.sub_projects ?? [];

  return (
    <div className={styles.planCard}>
      <div className={styles.planCardHead}>
        <Badge tone="neutral">Plan</Badge>
        <span className={styles.planCardTitle}>
          {plan?.project_name ?? job.route_target_name ?? "Planned project"}
        </span>
      </div>
      {plan?.description ? (
        <p className={styles.planCardDescription}>{plan.description}</p>
      ) : null}
      {tasks.length ? (
        <ul className={styles.planCardList}>
          {tasks.map((task, i) => (
            <li key={`${task.title ?? "task"}-${i}`}>{task.title}</li>
          ))}
        </ul>
      ) : null}
      {subProjects.map((sub) => (
        <div key={sub.name ?? "sub"} className={styles.planCardSub}>
          <span className={styles.planCardSubName}>{sub.name}</span>
          <ul className={styles.planCardList}>
            {(sub.tasks ?? []).map((task, i) => (
              <li key={`${task.title ?? "task"}-${i}`}>{task.title}</li>
            ))}
          </ul>
        </div>
      ))}
      <div className={styles.planCardActions}>
        <Select
          aria-label="Project"
          value={override}
          onChange={setOverride}
          options={[
            { value: "auto", label: `Auto (${job.route_target_name ?? "router decides"})` },
            ...projectOptions,
          ]}
        />
        <Button
          variant="primary"
          size="sm"
          disabled={confirming || busy}
          onClick={() => {
            setConfirming(true);
            void onConfirm(override).finally(() => setConfirming(false));
          }}
        >
          {confirming ? "Confirming…" : "Approve & run"}
        </Button>
        <Button variant="ghost" size="sm" onClick={onDiscard} disabled={confirming}>
          Discard
        </Button>
      </div>
    </div>
  );
}
