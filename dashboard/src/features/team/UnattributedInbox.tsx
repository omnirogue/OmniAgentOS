"use client";

/**
 * UnattributedInbox (P5) — evidence a deterministic collector could not
 * attribute to a card (`GET /api/team/evidence/unattributed`). Each row gets
 * a task-picker; choosing a task PATCHes the evidence onto it
 * (`PATCH /api/team/evidence/{id}`). `actor` is hardcoded `"operator"` —
 * this dashboard has no per-browser identity, so every existing mutation
 * (`collabApi.postMessage`'s `from_agent`, etc.) uses the same literal.
 */

import { useEffect, useMemo, useState } from "react";
import { Badge, Button, EmptyState, ErrorState, Loading, Select } from "@/design";
import { collabApi } from "@/features/collab/client";
import type { LiveBoardTask } from "@/features/collab/types";
import { teamApi, TeamApiError } from "./client";
import { useUnattributedEvidence } from "./hooks";
import type { TeamEvidence } from "./types";
import styles from "./team.module.css";

const REATTRIBUTE_ACTOR = "operator";

function taskOptionLabel(task: LiveBoardTask): string {
  const prefix = task.ref ? `${task.ref} · ` : "";
  return `${prefix}${task.title}`;
}

function InboxRow({
  evidence,
  taskOptions,
  onReattributed,
}: {
  evidence: TeamEvidence;
  taskOptions: Array<{ value: string; label: string }>;
  onReattributed: (evidenceId: string) => void;
}) {
  const [taskId, setTaskId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const attach = async () => {
    if (!taskId) return;
    setBusy(true);
    setError(null);
    try {
      await teamApi.reattributeEvidence(evidence.id, taskId, REATTRIBUTE_ACTOR);
      onReattributed(evidence.id);
    } catch (reason) {
      setError(
        reason instanceof TeamApiError
          ? reason.message
          : reason instanceof Error
            ? reason.message
            : "Reattribution failed.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={styles.inboxRow}>
      <div className={styles.inboxMeta}>
        <span className={styles.inboxTitle}>{evidence.title || evidence.ref}</span>
        <span className={styles.inboxDetail}>
          {evidence.kind}
          {evidence.repo ? ` · ${evidence.repo}` : ""} · {evidence.ref}
        </span>
        {error ? <span className={styles.verifyError}>{error}</span> : null}
      </div>
      <div className={styles.inboxPicker}>
        <Select
          aria-label={`Attach ${evidence.title || evidence.ref} to a task`}
          placeholder="Choose a task…"
          value={taskId}
          onChange={setTaskId}
          options={taskOptions}
          disabled={busy}
        />
      </div>
      <Button variant="secondary" size="sm" disabled={!taskId || busy} onClick={() => void attach()}>
        {busy ? "Attaching…" : "Attach"}
      </Button>
    </div>
  );
}

export function UnattributedInbox() {
  const { items, loading, error, refresh } = useUnattributedEvidence();
  const [taskOptions, setTaskOptions] = useState<Array<{ value: string; label: string }>>([]);

  // The task-picker's candidate list — the live board, cheap and already
  // fetched elsewhere in this same page tree; no dedicated "list tasks"
  // endpoint exists for this narrower purpose.
  useEffect(() => {
    let active = true;
    void collabApi.liveBoard().then((tasks) => {
      if (!active) return;
      setTaskOptions(tasks.map((task) => ({ value: task.id, label: taskOptionLabel(task) })));
    }).catch(() => undefined);
    return () => { active = false; };
  }, []);

  const handleReattributed = () => {
    void refresh();
  };

  const count = items.length;

  const body = useMemo(() => {
    if (loading) return <Loading variant="skeleton" label="Loading unattributed evidence" lines={3} />;
    if (error) return <ErrorState message={error} onRetry={refresh} />;
    if (items.length === 0) {
      return <EmptyState title="Nothing to attribute" message="Every collected artifact has a task." />;
    }
    return (
      <div className={styles.inbox}>
        {items.map((evidence) => (
          <InboxRow
            key={evidence.id}
            evidence={evidence}
            taskOptions={taskOptions}
            onReattributed={handleReattributed}
          />
        ))}
      </div>
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading, error, items, taskOptions]);

  return (
    <div>
      {/* No heading here on purpose — the host page's own Section title
          already labels this block ("Unattributed evidence"); this is the
          count + body only, so opening it never shows the label twice. */}
      {count > 0 ? <Badge tone="warn">{count} awaiting attribution</Badge> : null}
      {body}
    </div>
  );
}
