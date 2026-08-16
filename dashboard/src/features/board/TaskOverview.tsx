"use client";

/**
 * TaskOverview (§2.6 Overview tab) — progress, stage, ETA, agents, acceptance,
 * blockers. ETA honesty (§3.10): null estimate renders "Estimating…" — never
 * a fabricated number.
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import { Badge, Button, Card, Dialog, EmptyState, Input, Select, StatusDot } from "@/design";
import type {
  BoardTaskSessions,
  LiveBoardTask,
  LonghaulTaskDetail,
} from "@/features/collab/types";
import type { EtaResponse } from "@/features/chats/chatApi";
import { teamApi, TeamApiError } from "@/features/team/client";
import { AUTOMATION_MATURITY_OPTIONS, completionStateOf, employeeName } from "@/features/team/types";
import type { TeamTaskAccountabilityFields } from "@/features/team/types";
import { attemptOutcome } from "./attemptOutcome";
import styles from "./board.module.css";
import teamStyles from "@/features/team/team.module.css";

const STAGES = ["queued", "planning", "running", "reviewing", "done"] as const;

function stageFor(task: LiveBoardTask): string {
  const state = task.work?.state ?? task.status;
  if (["completed", "done"].includes(state ?? "")) return "done";
  if (["validating", "reviewing", "revision_requested"].includes(state ?? "")) return "reviewing";
  if (["running", "resuming", "awaiting_approval", "in_progress"].includes(state ?? "")) return "running";
  if (["draft", "ready", "starting", "claimed"].includes(state ?? "")) return "planning";
  return "queued";
}

function etaLabel(eta: EtaResponse | null): string {
  if (!eta || eta.estimate_seconds === null) return "—";
  const seconds = eta.estimate_seconds;
  if (seconds < 60) return `est. ~${seconds}s`;
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `est. ~${minutes}m` : `est. ~${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function EtaLine({ eta }: { eta: EtaResponse | null }) {
  if (!eta || eta.estimate_seconds === null) {
    return (
      <span className={styles.etaValue} title="No basis for an estimate yet">
        — <span className={styles.muted}>Estimating…</span>
      </span>
    );
  }
  return (
    <span
      className={styles.etaValue}
      title={`basis: ${eta.basis} · n=${eta.sample_size} · confidence: ${eta.confidence}`}
    >
      {etaLabel(eta)}
    </span>
  );
}

export function TaskOverview({
  task,
  sessions,
  longhaul,
  eta,
  onVerifyChanged,
}: {
  task: LiveBoardTask;
  sessions: BoardTaskSessions | null;
  longhaul: LonghaulTaskDetail | null;
  eta: EtaResponse | null;
  /** Called after a successful Verify/Unverify — the caller refetches the
   * task so `verified_at`/`verified_by` here stay in sync with the server. */
  onVerifyChanged?: () => void;
}) {
  const [verifyBusy, setVerifyBusy] = useState(false);
  const [verifyError, setVerifyError] = useState<string | null>(null);
  const [failOpen, setFailOpen] = useState(false);
  const [failReason, setFailReason] = useState("");

  // Migration 132 widened `board_tasks` (automation maturity + verification
  // failure stamps); `LiveBoardTask` is out of this package's ownership and
  // has not been widened for them, so they are read through a local cast
  // instead — see `TeamTaskAccountabilityFields`'s docstring.
  const teamFields = task as LiveBoardTask & TeamTaskAccountabilityFields;
  const completion = completionStateOf(teamFields);

  const [automationMaturity, setAutomationMaturity] = useState(teamFields.automation_maturity ?? "");
  const [automationNote, setAutomationNote] = useState(teamFields.automation_note ?? "");
  const [automationBusy, setAutomationBusy] = useState(false);
  const [automationError, setAutomationError] = useState<string | null>(null);

  // Reset the local drafts when the drawer switches to a DIFFERENT card —
  // never on every re-render of the SAME card, which would clobber an
  // in-progress edit the moment a background poll refreshes `task`.
  useEffect(() => {
    setAutomationMaturity(teamFields.automation_maturity ?? "");
    setAutomationNote(teamFields.automation_note ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.id]);

  const handleVerify = async () => {
    setVerifyBusy(true);
    setVerifyError(null);
    try {
      await teamApi.verifyTask(task.id, "emp_owner");
      onVerifyChanged?.();
    } catch (reason) {
      setVerifyError(
        reason instanceof TeamApiError || reason instanceof Error
          ? reason.message
          : "Verify failed.",
      );
    } finally {
      setVerifyBusy(false);
    }
  };

  const handleUnverify = async () => {
    setVerifyBusy(true);
    setVerifyError(null);
    try {
      await teamApi.unverifyTask(task.id, "emp_owner");
      onVerifyChanged?.();
    } catch (reason) {
      setVerifyError(
        reason instanceof TeamApiError || reason instanceof Error
          ? reason.message
          : "Unverify failed.",
      );
    } finally {
      setVerifyBusy(false);
    }
  };

  const handleFailVerify = async () => {
    const reason = failReason.trim();
    if (!reason) return;
    setVerifyBusy(true);
    setVerifyError(null);
    try {
      await teamApi.verifyTask(task.id, { verifier: "emp_owner", outcome: "fail", reason });
      setFailOpen(false);
      setFailReason("");
      onVerifyChanged?.();
    } catch (reason_) {
      setVerifyError(
        reason_ instanceof TeamApiError || reason_ instanceof Error
          ? reason_.message
          : "Fail verification failed.",
      );
    } finally {
      setVerifyBusy(false);
    }
  };

  const saveAutomation = async (fields: { automation_maturity: string | null; automation_note: string }) => {
    setAutomationBusy(true);
    setAutomationError(null);
    try {
      await teamApi.updateTaskAutomation(task.id, fields);
      onVerifyChanged?.();
    } catch (reason) {
      setAutomationError(
        reason instanceof TeamApiError || reason instanceof Error
          ? reason.message
          : "Could not save automation maturity.",
      );
    } finally {
      setAutomationBusy(false);
    }
  };

  const handleAutomationMaturityChange = (value: string) => {
    setAutomationMaturity(value);
    void saveAutomation({ automation_maturity: value === "" ? null : value, automation_note: automationNote });
  };

  const handleAutomationNoteBlur = () => {
    const trimmed = automationNote.trim();
    if (trimmed === (teamFields.automation_note ?? "")) return;
    void saveAutomation({
      automation_maturity: automationMaturity === "" ? null : automationMaturity,
      automation_note: trimmed,
    });
  };

  const work = task.work ?? null;
  const done = work?.steps_done ?? 0;
  const total = work?.steps_total ?? 0;
  const pct = total > 0 ? Math.round((done / total) * 100) : task.status === "done" ? 100 : 0;
  const stage = stageFor(task);
  const blockers: string[] = [];
  if (task.pending_approval) blockers.push(`Approval needed: ${task.pending_approval.command}`);
  // The card face carries a one-line hint; this is where the whole thing lives —
  // the failure mode the lane recorded, its detail, which ledger said so, and when.
  const blockedReason = task.status === "blocked" ? task.blocked_reason ?? null : null;
  if (blockedReason) {
    const parts = [blockedReason.reason.replace(/_/g, " ")];
    if (blockedReason.detail?.trim()) parts.push(blockedReason.detail.trim());
    const attribution = [blockedReason.source, blockedReason.at].filter(Boolean).join(" · ");
    blockers.push(
      `${parts.join(" — ")}${attribution ? ` (${attribution})` : ""}`,
    );
  }
  const workError = work?.error ?? task.run_error;
  // Without the guard the same failure appears twice: blocked_reason falls back
  // to exactly this text when the attempt ledger had nothing.
  if (workError && !(blockedReason && blockedReason.detail && workError.includes(blockedReason.detail))) {
    blockers.push(String(workError));
  }
  if (task.park_state) blockers.push(`Parked: ${task.park_state.replace(/_/g, " ")}`);

  return (
    <div className={styles.overviewGrid}>
      <Card padding="sm">
        <div className={styles.progressHead}>
          <strong>{total > 0 ? `${done}/${total} steps` : task.status.replace(/_/g, " ")}</strong>
          <span className={styles.muted}>{total > 0 ? `${pct}%` : ""}</span>
        </div>
        <div className={styles.progressTrack}>
          <div
            className={styles.progressFill}
            style={{ "--pct": `${pct}%` } as React.CSSProperties}
            role="progressbar"
            aria-valuenow={pct}
            aria-valuemin={0}
            aria-valuemax={100}
          />
        </div>
        {work?.current_step ? (
          <p className={styles.muted}>Now: {work.current_step}</p>
        ) : null}
      </Card>

      <Card padding="sm">
        <ol className={styles.stageStepper} aria-label="Task stage">
          {STAGES.map((id) => {
            const reached = STAGES.indexOf(id) <= STAGES.indexOf(stage as (typeof STAGES)[number]);
            return (
              <li
                key={id}
                className={`${styles.stageStep} ${reached ? styles.stageStepReached : ""} ${
                  id === stage ? styles.stageStepCurrent : ""
                }`}
              >
                {id}
              </li>
            );
          })}
        </ol>
        <div className={styles.etaRow}>
          <span className={styles.muted}>ETA</span>
          <EtaLine eta={eta} />
        </div>
        {work?.cost_usd !== null && work?.cost_usd !== undefined ? (
          <div className={styles.etaRow}>
            <span className={styles.muted}>Cost</span>
            <span>${work.cost_usd.toFixed(2)}</span>
          </div>
        ) : null}
      </Card>

      <Card padding="sm">
        <h4 className={styles.sectionTitle}>Agents</h4>
        {sessions && sessions.sessions.length ? (
          <div className={styles.agentList}>
            {sessions.sessions.map((session) => {
              const processIsLive = ["starting", "running", "resuming", "awaiting_approval"].includes(
                session.state,
              );
              const processDot = processIsLive
                ? "running"
                : session.state === "completed"
                  ? "completed"
                  : ["failed", "killed"].includes(session.state)
                    ? "failed"
                    : "queued";
              const outcome =
                session.source === "swarm"
                  ? attemptOutcome(session.end_reason, processIsLive)
                  : null;
              return (
                <div key={session.id} className={styles.agentRow}>
                  <StatusDot
                    state={processDot}
                    label={`Process ${session.state.replace(/_/g, " ")}`}
                  />
                  <strong>{session.model ?? session.provider ?? "agent"}</strong>
                  {outcome ? (
                    <>
                      <Badge tone={outcome.tone} category="result">
                        {outcome.label}
                      </Badge>
                      <span className={styles.muted}>
                        Process: {session.state.replace(/_/g, " ")}
                      </span>
                    </>
                  ) : (
                    <Badge tone="neutral">{session.state.replace(/_/g, " ")}</Badge>
                  )}
                  {session.tier ? <Badge tone="neutral">{session.tier}</Badge> : null}
                  {session.end_reason ? (
                    <span className={styles.muted}>
                      Reason: {session.end_reason.replace(/_/g, " ")}
                    </span>
                  ) : null}
                  {session.step_title ? (
                    <span className={styles.muted}>
                      {session.step_seq != null ? `Step ${session.step_seq}: ` : ""}
                      {session.step_title}
                    </span>
                  ) : null}
                  <Link className={styles.agentLink} href={`/activity/${session.id}?kind=session`}>
                    Transcript →
                  </Link>
                  {session.detail ? (
                    <div className={styles.agentDetail}>
                      <span className={styles.muted}>Verdict</span>
                      <span>{session.detail}</span>
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>
        ) : (
          <EmptyState message="No session has touched this card yet." />
        )}
      </Card>

      <Card padding="sm">
        <h4 className={styles.sectionTitle}>Acceptance criteria</h4>
        {longhaul?.acceptance ? (
          <pre className={styles.proseBlock}>{longhaul.acceptance}</pre>
        ) : task.acceptance_criteria ? (
          <pre className={styles.proseBlock}>{task.acceptance_criteria}</pre>
        ) : task.description ? (
          <pre className={styles.proseBlock}>{task.description}</pre>
        ) : (
          <EmptyState message="No acceptance criteria were recorded." />
        )}
      </Card>

      {/* Team Work OS fields (migration 123) — only present on a card a
          person owns, so the whole card is skipped for a plain agent task
          rather than showing an empty owner/ref/size shell. */}
      {task.owner_employee_id || task.ref || task.size ? (
        <Card padding="sm">
          <h4 className={styles.sectionTitle}>Team</h4>
          <div className={teamStyles.verifyRow}>
            {task.owner_employee_id ? (
              <span className={styles.muted}>
                Owner: {employeeName(task.owner_employee_id) ?? task.owner_employee_id}
              </span>
            ) : null}
            {task.ref ? <Badge tone="neutral">{task.ref}</Badge> : null}
            {task.size ? <Badge tone="neutral">{task.size}</Badge> : null}
          </div>
          <div className={`${teamStyles.verifyRow} ${teamStyles.verifyRowSpaced}`}>
            {completion === "verified" ? (
              <Badge tone="ok" title={task.verified_at ?? undefined}>
                Verified{task.verified_by ? ` by ${employeeName(task.verified_by) ?? task.verified_by}` : ""}
              </Badge>
            ) : completion === "failed_verification" ? (
              <Badge
                tone="danger"
                title={
                  [
                    teamFields.verification_failed_by
                      ? `by ${employeeName(teamFields.verification_failed_by) ?? teamFields.verification_failed_by}`
                      : null,
                    teamFields.verification_failed_at,
                  ]
                    .filter(Boolean)
                    .join(" · ") || undefined
                }
              >
                Verification failed
                {teamFields.verification_failed_reason ? `: ${teamFields.verification_failed_reason}` : ""}
              </Badge>
            ) : completion === "unverified" ? (
              <Badge tone="neutral">Unverified</Badge>
            ) : (
              <Badge tone="neutral">Not verified</Badge>
            )}
            {task.status === "done" ? (
              completion === "verified" ? (
                <Button variant="ghost" size="sm" disabled={verifyBusy} onClick={() => void handleUnverify()}>
                  {verifyBusy ? "Working…" : "Unverify"}
                </Button>
              ) : (
                <>
                  <Button variant="secondary" size="sm" disabled={verifyBusy} onClick={() => void handleVerify()}>
                    {verifyBusy ? "Working…" : "Verify"}
                  </Button>
                  <Button variant="ghost" size="sm" disabled={verifyBusy} onClick={() => setFailOpen(true)}>
                    Fail verification
                  </Button>
                </>
              )
            ) : null}
          </div>
          {verifyError ? <p className={teamStyles.verifyError}>{verifyError}</p> : null}

          {/* Automation maturity (migration 132, spec §9) — nullable, app-side
              validated vocabulary; saved via the collab PATCH (the only 131
              board_tasks column that IS directly patchable). */}
          <div className={`${teamStyles.verifyRow} ${teamStyles.verifyRowSpaced}`}>
            <Select
              label="Automation"
              value={automationMaturity}
              onChange={handleAutomationMaturityChange}
              options={AUTOMATION_MATURITY_OPTIONS}
              disabled={automationBusy}
            />
          </div>
          <Input
            label="Automation note"
            placeholder="What could the system do itself next time?"
            value={automationNote}
            onChange={(event) => setAutomationNote(event.target.value)}
            onBlur={handleAutomationNoteBlur}
            disabled={automationBusy}
          />
          {automationError ? <p className={teamStyles.verifyError}>{automationError}</p> : null}
        </Card>
      ) : null}

      <Dialog
        open={failOpen}
        onClose={() => {
          setFailOpen(false);
          setFailReason("");
        }}
        title="Fail verification"
        footer={
          <>
            <Button
              variant="ghost"
              onClick={() => {
                setFailOpen(false);
                setFailReason("");
              }}
            >
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void handleFailVerify()}
              disabled={!failReason.trim() || verifyBusy}
            >
              {verifyBusy ? "Working…" : "Fail verification"}
            </Button>
          </>
        }
      >
        <Input
          label="Reason (required)"
          value={failReason}
          onChange={(event) => setFailReason(event.target.value)}
          autoFocus
        />
      </Dialog>

      {blockers.length ? (
        <Card padding="sm" className={styles.blockerCard}>
          <h4 className={styles.sectionTitle}>Blockers</h4>
          <ul className={styles.blockerList}>
            {blockers.map((blocker) => (
              <li key={blocker}>{blocker}</li>
            ))}
          </ul>
        </Card>
      ) : null}
    </div>
  );
}
