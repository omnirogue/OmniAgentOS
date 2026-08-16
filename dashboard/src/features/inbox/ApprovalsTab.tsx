"use client";

import { useState } from "react";
import { Badge, Button, Card, Dialog, EmptyState, ErrorState, Input, Loading, Section, Table, type TableColumn } from "../../design";
import type { ActionClass, Approval } from "../../lib/contracts";
import { ArtifactPreview } from "../artifacts/ArtifactPreview";
import type { ApprovalsFeed } from "./useApprovalsFeed";
import inboxStyles from "./inbox.module.css";
import styles from "../ops/ops.module.css";

const actionTones: Record<ActionClass, "ok" | "challenger" | "promote" | "warn" | "danger"> = {
  read_only: "ok",
  sandboxed_creation: "challenger",
  internal_reversible: "promote",
  external_reversible: "warn",
  consequential: "danger",
  irreversible: "danger",
};

function truncate(value: string | null | undefined, limit = 100) {
  if (!value) return "-";
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}
function age(iso: string) {
  const minutes = Math.floor(Math.max(0, Date.now() - Date.parse(iso)) / 60000);
  return minutes < 1 ? "just now" : minutes < 60 ? `${minutes}m ago` : `${Math.floor(minutes / 60)}h ago`;
}
function date(value: string | null) {
  return value ? new Date(value).toLocaleString() : "-";
}
function ActionBadge({ actionClass }: { actionClass: ActionClass }) {
  return (
    <Badge category="disposition" tone={actionTones[actionClass]}>
      {actionClass}
    </Badge>
  );
}

function artifactPathFromApproval(approval: Approval): string | null {
  if (!approval.params_json) return null;
  try {
    const params = JSON.parse(approval.params_json) as Record<string, unknown>;
    for (const key of ["path", "file_path", "artifact_path", "dest", "output_path"]) {
      const v = params[key];
      if (typeof v === "string" && v.trim()) return v.trim();
    }
  } catch {
    /* ignore */
  }
  return null;
}

function toolPreview(approval: Approval) {
  if (!approval.params_json) return approval.proposed_action;
  try {
    const params = JSON.parse(approval.params_json) as unknown;
    return `${approval.proposed_action}: ${truncate(JSON.stringify(params), 80)}`;
  } catch {
    return approval.proposed_action;
  }
}
function Origin({ approval }: { approval: Approval }) {
  if (approval.session_id) {
    return (
      <span title={approval.session_id}>
        <Badge tone="warn">Session</Badge> {truncate(approval.session_id, 16)}
        <br />
        <small>{toolPreview(approval)}</small>
      </span>
    );
  }
  // /runs was retired in the P0 nav prune with no replacement detail page, so
  // this is plain text (still identifies the originating run) rather than a
  // dangling link.
  return approval.run_id ? <span title={approval.run_id}>{truncate(approval.run_id, 36)}</span> : "-";
}

/** The Inbox's default tab — kept functionally identical to the former
 * standalone `/approvals` page (same columns, same decide dialog + artifact
 * preview), just reading its data/mutations off the shared `feed` prop. */
export function ApprovalsTab({ feed }: { feed: ApprovalsFeed }) {
  const { pending, decided, loading, error, refresh, decide } = feed;
  const [deciding, setDeciding] = useState<{ approval: Approval; decision: "approved" | "rejected" } | null>(null);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  // Row-local decision failure (network blip, backend rejection, etc.) — distinct
  // from `feed.error` (the list-load error), which never fires for a decide call.
  // Mirrors the old standalone /approvals page's `decide()` catch, which set an
  // error the operator could see instead of a dialog that silently reopened.
  const [decisionError, setDecisionError] = useState<string | null>(null);

  // D-2 (LiveSim LS-004): `pending`/`decided` stay [] both while genuinely
  // empty AND while a fetch has failed before ever succeeding once -- `error`
  // is the only signal that distinguishes "confirmed empty" from "unknown".
  // Once either list has real (possibly stale) rows, a later error must not
  // erase them -- it just means the count/table may be stale, which the
  // ErrorState banner above already says.
  const pendingUnknown = Boolean(error) && pending.length === 0;
  const decidedUnknown = Boolean(error) && decided.length === 0;

  const openDecide = (approval: Approval, decision: "approved" | "rejected") => {
    setDecisionError(null);
    setDeciding({ approval, decision });
  };

  const closeDecide = () => {
    setDeciding(null);
    setDecisionError(null);
  };

  const submitDecision = async () => {
    if (!deciding) return;
    setSaving(true);
    try {
      await decide(deciding.approval, deciding.decision, note);
      setDeciding(null);
      setNote("");
      setDecisionError(null);
    } catch (reason) {
      // Keep the dialog open and the operator's note intact; surface the
      // failure inline so it is never silently discarded.
      setDecisionError(reason instanceof Error ? reason.message : "Could not save this decision.");
    } finally {
      setSaving(false);
    }
  };

  const pendingColumns: TableColumn<Approval>[] = [
    { key: "action", header: "Action class", sortable: true, sortValue: (r) => r.action_class, render: (r) => <ActionBadge actionClass={r.action_class} /> },
    { key: "proposed", header: "Proposed action", render: (r) => r.proposed_action },
    { key: "risk", header: "Risk", render: (r) => r.risk },
    { key: "evidence", header: "Evidence", render: (r) => truncate(r.evidence) },
    { key: "age", header: "Age", sortable: true, sortValue: (r) => Date.parse(r.created_at), render: (r) => <time dateTime={r.created_at}>{age(r.created_at)}</time> },
    { key: "origin", header: "Origin", render: (r) => <Origin approval={r} /> },
    {
      key: "decision",
      header: "Decision",
      render: (r) => {
        const busy = saving && deciding?.approval.id === r.id;
        return (
          <div className={styles.dialogActions}>
            <Button size="sm" variant="primary" disabled={busy} onClick={() => openDecide(r, "approved")}>
              {busy ? "Saving…" : "Approve"}
            </Button>
            <Button size="sm" variant="danger" disabled={busy} onClick={() => openDecide(r, "rejected")}>
              {busy ? "Saving…" : "Reject"}
            </Button>
          </div>
        );
      },
    },
  ];
  const decidedColumns: TableColumn<Approval>[] = [
    { key: "action", header: "Action class", render: (r) => <ActionBadge actionClass={r.action_class} /> },
    { key: "proposed", header: "Proposed action", render: (r) => r.proposed_action },
    { key: "origin", header: "Origin", render: (r) => <Origin approval={r} /> },
    {
      key: "state",
      header: "State",
      render: (r) => <Badge tone={r.state === "approved" ? "completed" : r.state === "rejected" ? "failed" : "warn"}>{r.state}</Badge>,
    },
    { key: "by", header: "Decided by", render: (r) => r.decided_by ?? "-" },
    { key: "note", header: "Note", render: (r) => truncate(r.decision_note) },
    { key: "at", header: "Decided at", render: (r) => date(r.decided_at) },
  ];

  return (
    <div>
      {loading && !pending.length ? <Loading label="Loading approvals…" /> : null}
      {error ? <ErrorState message={`Could not load approvals: ${error}`} onRetry={() => void refresh()} /> : null}

      <Section title={`Pending (${pendingUnknown ? "—" : pending.length})`}>
        {pendingUnknown ? null : !loading && !error && !pending.length ? (
          <Card>
            <EmptyState message="No approvals pending." />
          </Card>
        ) : (
          <Card padding="none">
            <Table columns={pendingColumns} rows={pending} rowKey={(r) => r.id} caption="Pending approvals" />
          </Card>
        )}
      </Section>

      <Section title="Recently decided">
        {decidedUnknown ? null : !loading && !error && !decided.length ? (
          <Card>
            <EmptyState message="No recent decisions." />
          </Card>
        ) : (
          <Card padding="none">
            <Table columns={decidedColumns} rows={decided} rowKey={(r) => r.id} caption="Recently decided approvals" />
          </Card>
        )}
      </Section>

      <Dialog
        open={!!deciding}
        onClose={() => {
          if (!saving) closeDecide();
        }}
        title={deciding?.decision === "approved" ? "Approve action" : "Reject action"}
        footer={
          <div className={styles.dialogActions}>
            <Button variant="ghost" onClick={closeDecide} disabled={saving}>
              Cancel
            </Button>
            <Button variant={deciding?.decision === "approved" ? "primary" : "danger"} onClick={() => void submitDecision()} disabled={saving}>
              {saving ? "Saving…" : "Confirm"}
            </Button>
          </div>
        }
      >
        <p>Add an optional note for this decision.</p>
        {deciding ? (
          <div className={inboxStyles.dialogNote}>
            <ArtifactPreview path={artifactPathFromApproval(deciding.approval)} />
          </div>
        ) : null}
        <Input label="Note" value={note} onChange={(event) => setNote(event.target.value)} autoFocus />
        {decisionError ? (
          <p className={inboxStyles.decisionError} role="alert">
            {decisionError}
          </p>
        ) : null}
      </Dialog>
    </div>
  );
}
