"use client";

import Link from "next/link";
import { useState } from "react";
import { Badge, Button, Card, CodeBlock, Dialog, Icon, Input, useToast } from "../../../design";
import {
  actionClassTone,
  confirmationFriction,
  formatDateTime,
  riskClassWarning,
  riskTone,
  suggestionStateTone,
} from "../format";
import type { ApproveSuggestionResult, ProposedPlan, Suggestion } from "../types";
import styles from "../steward.module.css";

export type SuggestionCardProps = {
  suggestion: Suggestion;
  onApprove: (id: string, approvedBy: string) => Promise<ApproveSuggestionResult>;
  onDismiss: (id: string, decidedBy: string, reason?: string) => Promise<Suggestion>;
};

/** The exact prompt/harness/action_class + evidence that motivated this suggestion —
 * shown BOTH on the card and repeated inside the approve dialog (DES-001) so the
 * operator can read what will run before, and at the moment of, approving. */
function PlanDetails({ plan, evidence }: { plan: ProposedPlan | null; evidence: unknown[] }) {
  return (
    <div className={styles.planBlock}>
      <div className={styles.row}>
        <Badge tone="neutral">harness: {plan?.harness || "unknown"}</Badge>
        <Badge tone={actionClassTone(plan?.action_class ?? "")}>
          action: {(plan?.action_class ?? "unknown").replace(/_/g, " ")}
        </Badge>
      </div>
      <div>
        <p className={styles.planLabel}>Prompt that will be dispatched to a live agent</p>
        {plan?.prompt ? (
          <CodeBlock maxHeight="16rem">{plan.prompt}</CodeBlock>
        ) : (
          <p className={styles.muted}>No plan/prompt is attached to this suggestion yet.</p>
        )}
      </div>
      {evidence.length > 0 ? (
        <div>
          <p className={styles.planLabel}>Evidence</p>
          <CodeBlock maxHeight="10rem">{JSON.stringify(evidence, null, 2)}</CodeBlock>
        </div>
      ) : null}
    </div>
  );
}

export function SuggestionCard({ suggestion, onApprove, onDismiss }: SuggestionCardProps) {
  const { push } = useToast();
  const [approveOpen, setApproveOpen] = useState(false);
  const [dismissOpen, setDismissOpen] = useState(false);
  const [approvedBy, setApprovedBy] = useState("");
  const [decidedBy, setDecidedBy] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [ackChecked, setAckChecked] = useState(false);
  const [typedTitle, setTypedTitle] = useState("");

  const isOpen = suggestion.state === "open";
  const friction = confirmationFriction(suggestion.risk_class);
  const warning = riskClassWarning(suggestion.risk_class);

  // DES-002: friction scales with risk_class — a benign suggestion only needs a
  // name, but an elevated one requires the matching extra step to be satisfied
  // before Approve enables. A reflex click can never approve a consequential action.
  const canApprove =
    approvedBy.trim().length > 0 &&
    (friction !== "checkbox" || ackChecked) &&
    (friction !== "type_confirm" || typedTitle.trim() === suggestion.title.trim());

  const submitApprove = async () => {
    const name = approvedBy.trim();
    if (!name || !canApprove) return;
    setBusy(true);
    try {
      await onApprove(suggestion.id, name);
      setApproveOpen(false);
      push({ title: "Suggestion approved", message: `${suggestion.title} is now running.`, tone: "success" });
    } catch (reasonErr) {
      push({ title: "Approve failed", message: reasonErr instanceof Error ? reasonErr.message : "Could not approve this suggestion.", tone: "error" });
    } finally {
      setBusy(false);
    }
  };

  const submitDismiss = async () => {
    const name = decidedBy.trim();
    if (!name) return;
    setBusy(true);
    try {
      await onDismiss(suggestion.id, name, reason.trim() || undefined);
      setDismissOpen(false);
      push({ title: "Suggestion dismissed", message: suggestion.title, tone: "info" });
    } catch (reasonErr) {
      push({ title: "Dismiss failed", message: reasonErr instanceof Error ? reasonErr.message : "Could not dismiss this suggestion.", tone: "error" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className={styles.suggestionCard}>
      <div className={styles.suggestionHead}>
        <div>
          <h3 className={styles.suggestionTitle}>{suggestion.title}</h3>
          <p className={styles.suggestionRationale}>{suggestion.rationale}</p>
        </div>
        <div className={styles.row}>
          <Badge tone={riskTone(suggestion.risk_class)}>{suggestion.risk_class.replace(/_/g, " ")}</Badge>
          <Badge tone={suggestionStateTone(suggestion.state)}>{suggestion.state}</Badge>
        </div>
      </div>

      <div className={styles.suggestionMeta}>
        <span>{suggestion.source}</span>
        <span>{formatDateTime(suggestion.created_at)}</span>
        {suggestion.decided_by ? <span>decided by {suggestion.decided_by}</span> : null}
      </div>

      {/* DES-001: the operator must be able to read what will run BEFORE approving —
          shown directly on the card, not hidden behind the dialog. */}
      <PlanDetails plan={suggestion.proposed_plan} evidence={suggestion.evidence} />

      {suggestion.state === "approved" && suggestion.run_id ? (
        <Link href={`/runs/${encodeURIComponent(suggestion.run_id)}`} className={styles.faint} style={{ color: "var(--accent)" }}>
          View run {suggestion.run_id} →
        </Link>
      ) : null}

      {isOpen ? (
        <div className={styles.suggestionActions}>
          <Button
            variant="primary"
            size="sm"
            onClick={() => {
              setApprovedBy("");
              setAckChecked(false);
              setTypedTitle("");
              setApproveOpen(true);
            }}
          >
            Approve
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setDecidedBy("");
              setReason("");
              setDismissOpen(true);
            }}
          >
            Dismiss
          </Button>
        </div>
      ) : null}

      <Dialog open={approveOpen} onClose={() => setApproveOpen(false)} title="Approve suggestion">
        <div className={styles.decisionForm}>
          <p className={styles.muted}>
            This turns “{suggestion.title}” into a real task and run at the current autonomy rung.
          </p>

          {/* The risk badge is repeated at the point of action, not just on the card. */}
          <div className={styles.row}>
            <Badge tone={riskTone(suggestion.risk_class)}>{suggestion.risk_class.replace(/_/g, " ")}</Badge>
          </div>

          {warning ? (
            <div className={styles.dangerBanner} role="alert">
              <Icon name="alertTriangle" size={16} />
              <span>{warning}</span>
            </div>
          ) : null}

          <PlanDetails plan={suggestion.proposed_plan} evidence={suggestion.evidence} />

          <Input
            label="Your name (approved_by)"
            value={approvedBy}
            onChange={(event) => setApprovedBy(event.target.value)}
            placeholder="e.g. owner"
            autoFocus
          />

          {friction === "checkbox" ? (
            <label className={styles.ackRow}>
              <input
                type="checkbox"
                checked={ackChecked}
                onChange={(event) => setAckChecked(event.target.checked)}
              />
              I have reviewed the plan above and acknowledge this action is not read-only.
            </label>
          ) : null}

          {friction === "type_confirm" ? (
            <Input
              label={`Type the suggestion title to confirm: "${suggestion.title}"`}
              value={typedTitle}
              onChange={(event) => setTypedTitle(event.target.value)}
              placeholder={suggestion.title}
              hint="This is a consequential action — typing the exact title proves you read it."
            />
          ) : null}

          <div className={styles.suggestionActions}>
            <Button variant="ghost" onClick={() => setApproveOpen(false)} disabled={busy}>Cancel</Button>
            <Button variant="primary" onClick={() => void submitApprove()} disabled={busy || !canApprove}>
              {busy ? "Approving…" : "Approve"}
            </Button>
          </div>
        </div>
      </Dialog>

      <Dialog open={dismissOpen} onClose={() => setDismissOpen(false)} title="Dismiss suggestion">
        <div className={styles.decisionForm}>
          <Input
            label="Your name (decided_by)"
            value={decidedBy}
            onChange={(event) => setDecidedBy(event.target.value)}
            placeholder="e.g. owner"
            autoFocus
          />
          <Input
            label="Reason (optional)"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Not a priority right now"
          />
          <div className={styles.suggestionActions}>
            <Button variant="ghost" onClick={() => setDismissOpen(false)} disabled={busy}>Cancel</Button>
            <Button variant="danger" onClick={() => void submitDismiss()} disabled={busy || !decidedBy.trim()}>
              {busy ? "Dismissing…" : "Dismiss"}
            </Button>
          </div>
        </div>
      </Dialog>
    </Card>
  );
}
