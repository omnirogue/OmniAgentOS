"use client";

import { useState } from "react";
import { Badge, Button, Card, Dialog, Input, Select } from "../../design";
import type { BadgeTone } from "../../design";
import { confirmationFriction, riskClassWarning } from "../steward/format";
import type { AvailableAction, DecideBody, Decision } from "./types";
import styles from "./decisions.module.css";

const CLASSIFICATION_TONE: Record<Decision["classification"], BadgeTone> = {
  urgent: "danger",
  needs_owner: "awaiting",
  maybe: "neutral",
};

function relativeAge(iso: string): string {
  const minutes = Math.floor(Math.max(0, Date.now() - Date.parse(iso)) / 60000);
  if (!Number.isFinite(minutes)) return "";
  return minutes < 1 ? "just now" : minutes < 60 ? `${minutes}m ago` : `${Math.floor(minutes / 60)}h ago`;
}
function whenLabel(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

/** Verification badge (§7): a green "done" is shown ONLY for a verified outcome;
 * `done_unverified` is honest amber — activity is not outcome. */
function verificationTone(status: Decision["status"]): BadgeTone {
  if (status === "done_verified") return "ok";
  if (status === "done_unverified") return "warn";
  if (status === "denied" || status === "expired") return "danger";
  return "neutral";
}

const RESOLVED_STATUSES = new Set<Decision["status"]>([
  "done_verified",
  "done_unverified",
  "dismissed",
  "denied",
  "expired",
]);

// One-click actions: no input needed, not consequential, and recoverable — so they
// fire immediately on click instead of opening a dialog. Dismissing is a low-stakes
// learning signal (the decision row survives as `dismissed`), and forcing a modal +
// Confirm + a "note" field on it is exactly the friction this system should not add.
const INSTANT_ACTIONS = new Set<AvailableAction["action"]>(["dismiss"]);

export interface DecisionCardProps {
  decision: Decision;
  /** The ONE mutation — POST decide. Rejects surface inline in the dialog. */
  onDecide: (id: string, body: DecideBody) => Promise<Decision>;
  /** Compact rows for the collapsed MAYBE / Snoozed sections. */
  compact?: boolean;
}

export function DecisionCard({ decision, onDecide, compact = false }: DecisionCardProps) {
  const [active, setActive] = useState<AvailableAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [instantError, setInstantError] = useState<string | null>(null);
  const resolved = RESOLVED_STATUSES.has(decision.status);

  // Fire a no-input action in one click. On failure, surface it inline (a 409 means it
  // was already handled elsewhere); never silently drop it.
  const runInstant = async (offer: AvailableAction) => {
    setBusy(true);
    setInstantError(null);
    try {
      await onDecide(decision.id, { action: offer.action });
    } catch (reason) {
      const status = (reason as { status?: number } | null)?.status;
      setInstantError(
        status === 409
          ? "Already handled elsewhere — refresh."
          : reason instanceof Error
            ? reason.message
            : "Could not save. Try again.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <div className={styles.cardHead}>
        <div>
          <div className={styles.titleRow}>
            {decision.number > 0 ? <span className={styles.ref}>EDC-{decision.number}</span> : null}
            <strong>{decision.title || "(untitled decision)"}</strong>
          </div>
          <div className={styles.badges}>
            <Badge tone={CLASSIFICATION_TONE[decision.classification]}>{decision.classification.replace("_", " ")}</Badge>
            {decision.deadline_at ? <Badge tone="warn">due {whenLabel(decision.deadline_at)}</Badge> : null}
            <Badge tone="neutral">{decision.risk_class}</Badge>
            {resolved ? <Badge tone={verificationTone(decision.status)}>{decision.status.replace(/_/g, " ")}</Badge> : null}
          </div>
        </div>
        <span className={styles.age}>{relativeAge(decision.created_at)}</span>
      </div>

      <p className={styles.source}>
        {decision.source_account || decision.source || "source"}
        {decision.counterparty ? ` · ${decision.counterparty}` : ""}
      </p>
      {decision.reason && !compact ? <p className={styles.reason}>{decision.reason}</p> : null}

      {/* Dominant recommended-action block; the sentinel (invariant 1) is loud. */}
      <div className={decision.recommended.missing ? styles.recommendMissing : styles.recommend}>
        <span className={styles.recommendLabel}>Recommended</span>
        <span>{decision.recommended.human_line}</span>
      </div>

      {/* Draft panel — the FE's SOLE send affordance, only while draft_pending (§6.4). */}
      {decision.status === "draft_pending" && decision.draft ? (
        <div className={styles.draft}>
          <div className={styles.draftMeta}>
            <span>To: {decision.draft.to || "—"}</span>
            <span>Subject: {decision.draft.subject || "—"}</span>
          </div>
          <pre className={styles.draftBody}>{decision.draft.body}</pre>
        </div>
      ) : null}

      {decision.verification && resolved ? (
        <p className={styles.verification}>
          <Badge tone={verificationTone(decision.status)}>{decision.verification.state}</Badge>{" "}
          {decision.verification.detail}
        </p>
      ) : null}

      {decision.notes ? <p className={styles.notes}>Note: {decision.notes}</p> : null}

      {/* Action row rendered EXACTLY from server-curated available_actions. */}
      {decision.available_actions.length > 0 ? (
        <div className={styles.actions}>
          {decision.available_actions.map((offer) => (
            <Button
              key={`${offer.action}:${offer.target ?? ""}`}
              size="sm"
              variant={offer.action === "approve" ? "primary" : offer.action === "deny" ? "danger" : "ghost"}
              onClick={() => setActive(offer)}
            >
              {offer.label}
            </Button>
          ))}
        </div>
      ) : null}

      {active ? (
        <ActionDialog
          decision={decision}
          offer={active}
          onClose={() => setActive(null)}
          onSubmit={(body) => onDecide(decision.id, body)}
        />
      ) : null}
    </Card>
  );
}

interface ActionDialogProps {
  decision: Decision;
  offer: AvailableAction;
  onClose: () => void;
  onSubmit: (body: DecideBody) => Promise<Decision>;
}

/** ONE generic decide dialog for all nine verbs — the body varies by
 * `offer.action`, the mutation is identical. There is no "your name" input
 * anywhere: attribution is the principal's, server-side. */
function ActionDialog({ decision, offer, onClose, onSubmit }: ActionDialogProps) {
  const { action, target } = offer;
  const [note, setNote] = useState("");
  const [intent, setIntent] = useState("");
  const [edited, setEdited] = useState(decision.recommended.missing ? "" : decision.recommended.human_line);
  const [assignee, setAssignee] = useState(decision.assignees[0]?.employee_id ?? "");
  const [snoozeUntil, setSnoozeUntil] = useState(decision.suggested_snoozes[0]?.until ?? "");
  const [ackDeadline, setAckDeadline] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [ackRisk, setAckRisk] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Approve of a draft = the send affordance; carry the sha so an edit voids it.
  const isDraftApprove = action === "approve" && decision.status === "draft_pending" && Boolean(decision.draft);
  const friction = confirmationFriction(decision.risk_class);
  const consequential = action === "execute" || isDraftApprove;
  const selectedSnooze = decision.suggested_snoozes.find((option) => option.until === snoozeUntil);
  const pastDeadline = Boolean(selectedSnooze?.past_deadline);

  const blocked =
    (action === "delegate" && !assignee) ||
    (action === "reply" && !intent.trim()) ||
    (action === "edit" && !edited.trim()) ||
    (action === "snooze" && (!snoozeUntil || (pastDeadline && !ackDeadline))) ||
    (consequential && friction === "checkbox" && !ackRisk) ||
    (consequential && friction === "type_confirm" && confirmText.trim() !== decision.title.trim());

  const submit = async () => {
    setSaving(true);
    setError(null);
    const body: DecideBody = { action };
    if (note.trim()) body.note = note.trim();
    const params: NonNullable<DecideBody["params"]> = {};
    if (action === "delegate") params.assignee = assignee;
    if (action === "reply") params.intent = intent.trim();
    if (action === "edit") {
      params.edited_recommendation = edited.trim();
      if (decision.classification === "maybe" && target === "needs_owner") params.reclassify = "needs_owner";
    }
    if (action === "snooze") {
      params.until = snoozeUntil;
      if (pastDeadline) params.acknowledge_deadline = true;
    }
    if (action === "defer" && target) params.defer_target = target;
    if (isDraftApprove && decision.draft) params.draft_sha256 = decision.draft.sha256;
    if (consequential) params.confirm = true;
    if (Object.keys(params).length > 0) body.params = params;

    try {
      await onSubmit(body);
      onClose();
    } catch (reason) {
      // Keep the dialog open + inputs preserved; surface the failure inline —
      // a failed action is never silently discarded (kimi-fe). A 409 means the
      // row was already handled elsewhere.
      const status = (reason as { status?: number } | null)?.status;
      setError(
        status === 409
          ? "Already handled — this decision was resolved elsewhere (Slack or another tab). Close and refresh."
          : reason instanceof Error
            ? reason.message
            : "Could not save this decision.",
      );
    } finally {
      setSaving(false);
    }
  };

  const warning = consequential ? riskClassWarning(decision.risk_class) : null;

  return (
    <Dialog
      open
      onClose={() => {
        if (!saving) onClose();
      }}
      title={`${offer.label} — ${decision.title || `EDC-${decision.number}`}`}
      footer={
        <div className={styles.dialogActions}>
          <Button variant="ghost" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button
            variant={action === "deny" ? "danger" : "primary"}
            onClick={() => void submit()}
            disabled={saving || blocked}
          >
            {saving ? "Saving…" : isDraftApprove ? "Approve & send" : "Confirm"}
          </Button>
        </div>
      }
    >
      {warning ? (
        <p className={styles.dangerBanner} role="alert">
          {warning}
        </p>
      ) : null}

      {action === "delegate" ? (
        decision.assignees.length > 0 ? (
          <Select
            label="Assign to"
            options={decision.assignees.map((a) => ({ value: a.employee_id, label: a.name }))}
            value={assignee}
            onChange={setAssignee}
          />
        ) : (
          <p className={styles.hint}>No one is available to assign this to.</p>
        )
      ) : null}

      {action === "defer" ? (
        <p className={styles.hint}>
          Defer to {target === "machine" ? "the machine work queue" : "the shared work queue"}.
        </p>
      ) : null}

      {action === "reply" ? (
        <label className={styles.field}>
          <span>What should the reply say?</span>
          <textarea className={styles.textarea} value={intent} onChange={(e) => setIntent(e.target.value)} rows={4} autoFocus />
          <span className={styles.hint}>This drafts a reply for your review — it does not send.</span>
        </label>
      ) : null}

      {action === "edit" ? (
        <label className={styles.field}>
          <span>{target === "needs_owner" ? "Promote to Needs-me — refine the action" : "Edit the recommended action"}</span>
          <textarea className={styles.textarea} value={edited} onChange={(e) => setEdited(e.target.value)} rows={4} autoFocus />
        </label>
      ) : null}

      {action === "snooze" ? (
        <div className={styles.field}>
          {decision.suggested_snoozes.length > 0 ? (
            <Select
              label="Snooze until"
              options={decision.suggested_snoozes.map((option) => ({
                value: option.until,
                label: `${option.label}${option.past_deadline ? " ⚠ past deadline" : ""}`,
              }))}
              value={snoozeUntil}
              onChange={setSnoozeUntil}
            />
          ) : null}
          <Input
            label="Or a specific time"
            type="datetime-local"
            value={snoozeUntil}
            onChange={(e) => setSnoozeUntil(e.target.value)}
          />
          {pastDeadline ? (
            <label className={styles.checkRow}>
              <input type="checkbox" checked={ackDeadline} onChange={(e) => setAckDeadline(e.target.checked)} />
              <span className={styles.dangerText}>
                This snoozes past the deadline ({whenLabel(decision.deadline_at)}). I understand.
              </span>
            </label>
          ) : null}
        </div>
      ) : null}

      {consequential && friction === "type_confirm" ? (
        <Input
          label={`Type the title to confirm: “${decision.title}”`}
          value={confirmText}
          onChange={(e) => setConfirmText(e.target.value)}
          autoFocus
        />
      ) : null}
      {consequential && friction === "checkbox" ? (
        <label className={styles.checkRow}>
          <input type="checkbox" checked={ackRisk} onChange={(e) => setAckRisk(e.target.checked)} />
          <span>I understand this takes effect immediately.</span>
        </label>
      ) : null}

      <Input label="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />

      {error ? (
        <p className={styles.decisionError} role="alert">
          {error}
        </p>
      ) : null}
    </Dialog>
  );
}
