"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Badge, Button, Card, Dialog, EmptyState, ErrorState, Icon, Input, Loading, Page, PageHeader, Pill, Section, StatusDot, Table, type BadgeTone, type PillTone, type StatusDotState, type TableColumn } from "../../design";
import { api } from "../../lib/api";
import type { Approval, Session } from "../../lib/contracts";
import { parseTranscript, type TranscriptEntry } from "../../lib/transcriptParser";
import { startVisibilityPoll } from "../../lib/pollWhenVisible";
import { decideFixtureApproval, killFixtureSession, updateFixtureSession, USE_FIXTURES } from "../../features/sessions/fixtures";
import { useSessions } from "../../features/sessions/hooks";
import { effectiveCompany, groupSessionsByCompany, isSessionActive, sessionsNeedingAttention, KNOWN_COMPANIES } from "../../features/sessions/grouping";
import { applyOverride, resolveSessionOverrides, settleOverride, type OverridableField, type SessionOverrides } from "../../features/sessions/optimisticOverrides";
import { createTranscriptPoller } from "../../features/sessions/transcriptPoller";
import { formatSessionActivity as activity, formatCost as cost, formatRelativeTime } from "../../lib/format";
import styles from "./sessions.module.css";

const dotStates: Record<Session["state"], StatusDotState> = {
  starting: "queued",
  running: "running",
  awaiting_approval: "awaiting_approval",
  resuming: "running",
  completed: "completed",
  failed: "failed",
  cancelled: "cancelled",
  killed: "danger",
};

const badgeTones: Record<Session["state"], BadgeTone> = {
  starting: "queued",
  running: "running",
  awaiting_approval: "awaiting_approval",
  resuming: "running",
  completed: "completed",
  failed: "failed",
  cancelled: "cancelled",
  killed: "danger",
};

function shortId(id: string) { return id.length > 18 ? `${id.slice(0, 17)}…` : id; }
function approvalCount(value: number | null | undefined) { return value == null ? "—" : value; }
function approvalInput(approval: Approval) {
  if (!approval.params_json) return approval.proposed_action;
  try { return `${approval.proposed_action} ${JSON.stringify(JSON.parse(approval.params_json))}`; } catch { return approval.proposed_action; }
}

function agentStatusTone(status: string): PillTone {
  if (status === "busy") return "accent";
  if (status === "idle") return "neutral";
  return "neutral";
}

// `agent_profile` and the session id/ref are machine-generated (profile dir
// names, uuids) so shell metacharacters are unlikely, but the attach command
// is rendered as literal text an operator copy-pastes into a shell — harden
// by validating both tokens against a conservative allow-list rather than
// attempting shell-quoting. A value outside this shape means `attachCommand`
// returns null and the caller omits the button instead of emitting a string
// that could be misread as multiple shell arguments.
const SAFE_ATTACH_TOKEN = /^[A-Za-z0-9._-]+$/;

/** `CLAUDE_CONFIG_DIR=~/.<agent_profile> claude attach <sessionId-or-session_ref>` — the
 * exact command an operator pastes locally to attach to this session's CLI harness.
 * Returns null when either token fails the safe-shell-token check. */
function attachCommand(session: Session): string | null {
  const profile = session.agent_profile;
  const target = session.session_ref ?? session.id;
  if (!profile || !SAFE_ATTACH_TOKEN.test(profile) || !SAFE_ATTACH_TOKEN.test(target)) return null;
  return `CLAUDE_CONFIG_DIR=~/.${profile} claude attach ${target}`;
}

const CUSTOM_COMPANY_VALUE = "__custom__";

/** Inline title editor. The existing "open detail" button is left untouched
 * (it is the only way into the transcript/approvals dialog); a small "Edit"
 * affordance in the same cell toggles a text input that posts to
 * POST /api/sessions/{id}/update. */
function TitleCell({ session, onOpen, onSave }: { session: Session; onOpen: () => void; onSave: (title: string) => Promise<void> }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(session.title ?? "");
  const [saving, setSaving] = useState(false);
  const label = session.title ?? shortId(session.id);

  if (!editing) {
    return (
      <span className={styles.titleCell}>
        <Button variant="ghost" size="sm" onClick={onOpen} aria-label={`View session ${label}`}>{label}</Button>
        <Button
          variant="ghost"
          size="sm"
          className={styles.editTitleBtn}
          aria-label={`Edit title for ${label}`}
          onClick={() => { setDraft(session.title ?? ""); setEditing(true); }}
        >
          Edit
        </Button>
      </span>
    );
  }

  return (
    <form
      className={styles.titleEditForm}
      onSubmit={(event) => {
        event.preventDefault();
        setSaving(true);
        void onSave(draft.trim()).finally(() => { setSaving(false); setEditing(false); });
      }}
    >
      <Input
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        aria-label={`Title for ${label}`}
        disabled={saving}
        autoFocus
      />
      <Button size="sm" variant="primary" type="submit" disabled={saving}>{saving ? "Saving…" : "Save"}</Button>
      <Button size="sm" variant="ghost" type="button" disabled={saving} onClick={() => setEditing(false)}>Cancel</Button>
    </form>
  );
}

/**
 * Company editor. A native <select> with the five known companies plus a
 * "clear the manual override" option and an "Other…" escape hatch into free
 * text — a plain <select> is simplest to drive from a table cell and matches
 * the pattern already used for row actions elsewhere
 * (features/chats/ChatSidebar.tsx).
 *
 * The first option clears `company_override` (posts `{company: null}`), NOT
 * the collector-assigned `company` field — that is intentional (see
 * features/sessions/grouping.ts effectiveCompany doc): an operator can
 * override the auto-detected company, but "clearing" always falls back to
 * the auto value rather than blanking it outright. The option is labelled
 * "Auto (<resolved company>)" when the session has one, so picking it reads
 * as "revert to auto" rather than "set to nothing".
 */
function CompanyCell({ session, onSave }: { session: Session; onSave: (company: string | null) => Promise<void> }) {
  const current = effectiveCompany(session) ?? "";
  const isKnown = current === "" || (KNOWN_COMPANIES as readonly string[]).includes(current);
  const [customMode, setCustomMode] = useState(!isKnown);
  const [draft, setDraft] = useState(current);
  const [saving, setSaving] = useState(false);
  const label = session.title ?? session.id;
  const autoCompany = session.company?.trim() || null;
  const clearOptionLabel = autoCompany ? `Auto (${autoCompany})` : "— none —";

  useEffect(() => {
    setDraft(current);
    setCustomMode(current !== "" && !(KNOWN_COMPANIES as readonly string[]).includes(current));
  }, [current]);

  const commit = (value: string) => {
    setSaving(true);
    void onSave(value.trim() ? value.trim() : null).finally(() => setSaving(false));
  };

  if (customMode) {
    return (
      <form
        className={styles.companyCustomForm}
        onSubmit={(event) => {
          event.preventDefault();
          commit(draft);
        }}
      >
        <Input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          aria-label={`Company for ${label}`}
          placeholder="Company name"
          disabled={saving}
        />
        <Button size="sm" type="submit" disabled={saving}>{saving ? "Saving…" : "Save"}</Button>
        <Button size="sm" variant="ghost" type="button" disabled={saving} onClick={() => { setCustomMode(false); setDraft(current); }}>Cancel</Button>
      </form>
    );
  }

  return (
    <select
      className={styles.companySelect}
      aria-label={`Company for ${label}`}
      value={current}
      disabled={saving}
      onChange={(event) => {
        const value = event.target.value;
        if (value === CUSTOM_COMPANY_VALUE) {
          setDraft("");
          setCustomMode(true);
          return;
        }
        commit(value);
      }}
    >
      <option value="">{clearOptionLabel}</option>
      {KNOWN_COMPANIES.map((name) => <option key={name} value={name}>{name}</option>)}
      <option value={CUSTOM_COMPANY_VALUE}>Other…</option>
    </select>
  );
}

/** Copy-to-clipboard helper for attaching a local CLI to this session's
 * harness. Renders nothing when the session carries no `agent_profile`; when
 * the profile/id fails the safe-token check, the button is omitted and a
 * tooltip explains why instead of emitting an unsafe shell string. */
function AttachHelper({ session }: { session: Session }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  if (!session.agent_profile) return null;
  const command = attachCommand(session);

  if (!command) {
    return (
      <div>
        <strong>Attach locally</strong>
        <p
          className={styles.mutedSub}
          title="This session's id or agent profile contains characters outside [A-Za-z0-9._-], so the attach command was withheld rather than risk an unsafe shell string."
        >
          Attach command unavailable — this session&apos;s id or agent profile isn&apos;t safe to paste into a shell.
        </p>
      </div>
    );
  }

  const copy = async () => {
    let ok = false;
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(command);
        ok = true;
      }
    } catch {
      ok = false;
    }
    setCopyState(ok ? "copied" : "failed");
    window.setTimeout(() => setCopyState("idle"), 2000);
  };

  return (
    <div>
      <strong>Attach locally</strong>
      <div className={styles.attachRow}>
        <code className={styles.attachCode}>{command}</code>
        <Button size="sm" variant="ghost" onClick={() => void copy()}>
          {copyState === "copied" ? "Copied!" : copyState === "failed" ? "Copy failed — select the text above" : "Copy"}
        </Button>
      </div>
    </div>
  );
}

export default function SessionsPage() {
  const { sessions, approvals, loading, error, unauthorized, refresh, connected } = useSessions();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [confirmKill, setConfirmKill] = useState<Session | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [transcriptLoading, setTranscriptLoading] = useState(false);
  const [transcriptError, setTranscriptError] = useState<string | null>(null);
  const [liveTail, setLiveTail] = useState(false);
  // Optimistic overlay for inline title/company edits: applied immediately on
  // submit, cleared per-field once that field's save settles (refresh() has
  // already replaced `sessions` with the server's view by then; on failure
  // this simply reverts to the last known-good value while `actionError`
  // explains why). Tracked per-field with a monotonic request id — see
  // features/sessions/optimisticOverrides.ts — so a title save settling never
  // wipes a still-in-flight company edit on the same row, and an older
  // request settling late never clobbers a newer edit on the same field.
  const [overrides, setOverrides] = useState<SessionOverrides>({});
  const editRequestSeq = useRef(0);
  // D-2 (LiveSim LS-004) audit find: `useSessions` never clears `sessions` on
  // a failed refresh (it keeps the last known-good rows), so `sessions.length
  // === 0` only means "unknown" when it is paired with `error` -- a genuine
  // empty roster never has `error` set. Distinguish "0 sessions ever
  // recorded" from "we don't know because the fetch failed."
  const sessionsUnknown = Boolean(error) && sessions.length === 0;
  const displaySessions = useMemo(
    () => sessions.map((session) => resolveSessionOverrides(session, overrides)),
    [sessions, overrides],
  );
  const [showFinished, setShowFinished] = useState(false);
  const activeSessions = useMemo(
    () =>
      displaySessions.filter(isSessionActive),
    [displaySessions],
  );
  const finishedCount = displaySessions.length - activeSessions.length;
  const visibleSessions = showFinished ? displaySessions : activeSessions;
  const selected = displaySessions.find((session) => session.id === selectedId) ?? null;
  const selectedApprovals = useMemo(() => selected ? approvals.filter((approval) => approval.session_id === selected.id && approval.state === "pending") : [], [approvals, selected]);
  const needsYou = useMemo(() => sessionsNeedingAttention(visibleSessions), [visibleSessions]);
  const companyGroups = useMemo(() => groupSessionsByCompany(visibleSessions), [visibleSessions]);

  useEffect(() => {
    if (!selectedId || USE_FIXTURES) {
      setTranscript([]);
      setTranscriptError(null);
      return;
    }
    let cancelled = false;
    setTranscriptLoading(true);
    setTranscriptError(null);
    void api
      .sessionTranscript(selectedId)
      .then((raw) => {
        if (cancelled) return;
        setTranscript(parseTranscript(raw));
      })
      .catch((reason) => {
        if (cancelled) return;
        setTranscript([]);
        setTranscriptError(reason instanceof Error ? reason.message : "Could not load conversation log.");
      })
      .finally(() => {
        if (!cancelled) setTranscriptLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  // Live tail mode (P3): while the detail dialog is open and this tab is
  // visible, re-poll the transcript every 4s and render the latest entries
  // through the same parseTranscript() the initial load uses. Reuses the
  // pollWhenVisible backstop pattern (useSessions' 30s poll); does NOT touch
  // SSE plumbing. The transcript endpoint returns a full snapshot (no server
  // sequence number) so createTranscriptPoller guards ordering itself: it
  // discards a response once a later tick has been dispatched (an earlier,
  // slower poll must not roll the transcript backward), and `stop()` on
  // effect cleanup discards any response that lands after the dialog closes,
  // the session switches, or live tail is turned off — the same "cancelled"
  // guard the initial transcript fetch effect above already uses.
  useEffect(() => {
    if (!selectedId) setLiveTail(false);
  }, [selectedId]);

  useEffect(() => {
    if (!selectedId || !liveTail || USE_FIXTURES) return;
    const poller = createTranscriptPoller({
      fetchTranscript: () => api.sessionTranscript(selectedId),
      onEntries: (raw) => setTranscript(parseTranscript(raw)),
    });
    const stopPolling = startVisibilityPoll(() => void poller.poll(), 4000);
    return () => {
      poller.stop();
      stopPolling();
    };
  }, [selectedId, liveTail]);

  const decide = async (approval: Approval, decision: "approved" | "rejected") => {
    setBusy(approval.id);
    try {
      if (USE_FIXTURES) decideFixtureApproval(approval.id, decision);
      else await api.decideApproval(approval.id, decision);
      await refresh();
      setActionError(null);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Unable to record decision.");
    } finally { setBusy(null); }
  };

  const kill = async () => {
    if (!confirmKill) return;
    setBusy(confirmKill.id);
    try {
      if (USE_FIXTURES) killFixtureSession(confirmKill.id);
      else await api.killSession(confirmKill.id);
      setConfirmKill(null);
      await refresh();
      setActionError(null);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Unable to stop this session.");
    } finally { setBusy(null); }
  };

  const updateSession = async (id: string, patch: { title?: string; company?: string | null }) => {
    // Request ids are assigned once, up front (not inside the setOverrides
    // updater) so they stay stable even if React invokes the updater more
    // than once (StrictMode double-invoke) — applyOverride is otherwise pure
    // given the same requestId.
    const requests: { field: OverridableField; requestId: number }[] = [];
    if (patch.title !== undefined) requests.push({ field: "title", requestId: ++editRequestSeq.current });
    if (patch.company !== undefined) requests.push({ field: "company_override", requestId: ++editRequestSeq.current });

    setOverrides((prev) => {
      let next = prev;
      for (const { field, requestId } of requests) {
        const value = field === "title" ? (patch.title ?? null) : (patch.company ?? null);
        next = applyOverride(next, id, field, value, requestId);
      }
      return next;
    });
    try {
      if (USE_FIXTURES) updateFixtureSession(id, patch);
      else await api.updateSession(id, patch);
      setActionError(null);
      await refresh();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "Unable to update this session.");
    } finally {
      setOverrides((prev) => requests.reduce((acc, { field, requestId }) => settleOverride(acc, id, field, requestId), prev));
    }
  };

  const columns: TableColumn<Session>[] = [
    {
      key: "session", header: "Session", sortable: true, sortValue: (row) => row.title ?? row.id,
      render: (row) => <TitleCell session={row} onOpen={() => setSelectedId(row.id)} onSave={(title) => updateSession(row.id, { title })} />,
    },
    {
      key: "company", header: "Company", sortable: true, sortValue: (row) => effectiveCompany(row) ?? "",
      render: (row) => <CompanyCell session={row} onSave={(company) => updateSession(row.id, { company })} />,
    },
    {
      key: "agent", header: "Agent", sortable: true, sortValue: (row) => row.agent_status ?? "",
      render: (row) => (
        <span className={styles.agentCell}>
          {row.agent_name ? <span>{row.agent_name}</span> : null}
          {row.agent_status ? <Pill tone={agentStatusTone(row.agent_status)}>{row.agent_status}</Pill> : null}
        </span>
      ),
    },
    {
      key: "state", header: "State", sortable: true, sortValue: (row) => row.state,
      render: (row) => <span className={styles.statusWrapper}><StatusDot state={dotStates[row.state]} label={row.state.replaceAll("_", " ")} /><Badge tone={badgeTones[row.state]}>{row.state.replaceAll("_", " ")}</Badge></span>,
    },
    { key: "source", header: "Source", sortable: true, sortValue: (row) => row.source, render: (row) => <Badge tone={row.source === "bridge" ? "promote" : "neutral"}>{row.source}</Badge> },
    { key: "activity", header: "Progress / activity", sortable: true, sortValue: (row) => row.last_activity_at ?? row.created_at, render: (row) => <span>{activity(row.last_activity_at)}<br /><small>{row.project_dir}</small></span> },
    { key: "cost", header: "Cost", align: "right", sortable: true, sortValue: (row) => row.cost_usd, render: (row) => cost(row.cost_usd) },
  ];

  const needsYouColumns: TableColumn<Session>[] = [
    {
      key: "session", header: "Session", sortable: true, sortValue: (row) => row.title ?? row.id,
      render: (row) => <Button variant="ghost" size="sm" onClick={() => setSelectedId(row.id)} aria-label={`View session ${row.title ?? row.id}`}>{row.title ?? shortId(row.id)}</Button>,
    },
    { key: "reason", header: "Needs", render: (row) => row.attention_reason ?? "Input required" },
    { key: "since", header: "Since", sortable: true, sortValue: (row) => row.attention_since ?? "", render: (row) => formatRelativeTime(row.attention_since ?? null) },
  ];

  return (
    <Page>
      <PageHeader eyebrow="Live work" title="Sessions" lead="Monitor bridge and external Claude Code sessions from one operator screen." actions={<Badge tone={connected ? "ok" : "neutral"}>{connected ? "Live events connected" : "Live events reconnecting…"}</Badge>} />

      {loading && !sessions.length ? <Loading variant="skeleton" label="Loading sessions…" lines={5} /> : null}
      {!loading && unauthorized ? (
        <Card>
          <EmptyState
            icon={<Icon name="plug" size={22} />}
            title="No local session token in this browser"
            message="Sessions and their approvals are only visible from the operator's authorized local session (GET /api/sessions requires the session token). This isn't a fetch error — nothing is broken — but this browser can't show live session data."
            action={<Button size="sm" onClick={() => void refresh()}>Check again</Button>}
          />
        </Card>
      ) : null}
      {error ? <ErrorState message={`Could not load sessions: ${error}`} onRetry={() => void refresh()} /> : null}
      {actionError ? <ErrorState title="Session action failed" message={actionError} onRetry={() => void refresh()} /> : null}

      {!unauthorized ? (
        <Section
          title={`${showFinished ? "All" : "Running"} sessions (${sessionsUnknown ? "\u2014" : visibleSessions.length})`}
          description="Select a session to view its recent activity and approval context."
          actions={
            finishedCount > 0 ? (
              <Button size="sm" variant="ghost" onClick={() => setShowFinished((value) => !value)}>
                {showFinished ? `Hide finished (${finishedCount})` : `Show finished (${finishedCount})`}
              </Button>
            ) : undefined
          }
        >
          {sessionsUnknown ? null : !loading && !error && !sessions.length ? <Card><EmptyState message="No sessions have been recorded." /></Card> : !visibleSessions.length ? <Card><EmptyState message={`No running sessions. ${finishedCount} finished session${finishedCount === 1 ? "" : "s"} hidden.`} /></Card> : (
            <div className={styles.groupsStack}>
              {needsYou.length > 0 ? (
                <div className={styles.groupSection}>
                  <h3 className={styles.groupHeading}>Needs you ({needsYou.length})</h3>
                  <Card padding="none"><Table columns={needsYouColumns} rows={needsYou} rowKey={(row) => row.id} caption="Sessions needing your input" /></Card>
                </div>
              ) : null}
              {companyGroups.map((group) => (
                <div key={group.company} className={styles.groupSection}>
                  <h3 className={styles.groupHeading}>{group.company} ({group.sessions.length})</h3>
                  <Card padding="none"><Table columns={columns} rows={group.sessions} rowKey={(row) => row.id} caption="Claude Code sessions" /></Card>
                </div>
              ))}
            </div>
          )}
        </Section>
      ) : null}

      <Dialog
        open={!!selected || !!confirmKill}
        onClose={() => {
          if (confirmKill) {
            if (!busy) setConfirmKill(null);
          } else {
            setSelectedId(null);
          }
        }}
        title={confirmKill ? "Kill this session" : selected?.title ?? selected?.id ?? "Session"}
        footer={confirmKill ? <div className={styles.footerButtons}><Button variant="ghost" disabled={!!busy} onClick={() => setConfirmKill(null)}>Cancel</Button><Button variant="danger" disabled={!!busy} onClick={() => void kill()}>{busy ? "Stopping…" : "Kill session"}</Button></div> : <div className={styles.footerButtons}><Button variant="ghost" onClick={() => setSelectedId(null)}>Close</Button><Button variant="danger" disabled={!selected || busy === selected.id || ["completed", "failed", "cancelled", "killed"].includes(selected?.state ?? "")} onClick={() => selected && setConfirmKill(selected)}>{busy === selected?.id ? "Stopping…" : "Kill session"}</Button></div>}
      >
        {confirmKill ? <p>This sends a stop signal to <strong>{confirmKill.title ?? confirmKill.id}</strong>. Any pending work will not continue.</p> : selected ? <div className={styles.detailsGrid}>
          <div className={styles.badgeRow}><StatusDot state={dotStates[selected.state]} label={selected.state.replaceAll("_", " ")} /><Badge tone={badgeTones[selected.state]}>{selected.state.replaceAll("_", " ")}</Badge><Badge tone={selected.source === "bridge" ? "promote" : "neutral"}>{selected.source}</Badge>{selected.agent_status ? <Pill tone={agentStatusTone(selected.agent_status)}>{selected.agent_status}</Pill> : null}</div>
          <div><strong>Project</strong><br />{selected.project_dir}</div>
          <div><strong>Provider / model</strong><br />{selected.provider}{selected.model ? ` · ${selected.model}` : ""}</div>
          <div><strong>Recent activity</strong><br />Created {new Date(selected.created_at).toLocaleString()} · {activity(selected.last_activity_at)} · Cost {cost(selected.cost_usd)}</div>
          <div><strong>Approvals</strong><br />{approvalCount(selected.approvals_requested)} requested · {approvalCount(selected.approvals_granted)} granted · {approvalCount(selected.approvals_denied)} denied</div>
          <div>
            <strong>Pending approval</strong>
            {selectedApprovals.length ? selectedApprovals.map((approval) => <Card key={approval.id} padding="sm" className={styles.approvalCard}><p><strong>{approval.proposed_action}</strong></p><p>{approvalInput(approval)}</p><p>{approval.risk}</p><div className={styles.approvalActions}><Button size="sm" variant="primary" disabled={busy === approval.id} onClick={() => void decide(approval, "approved")}>{busy === approval.id ? "Saving…" : "Approve"}</Button><Button size="sm" variant="danger" disabled={busy === approval.id} onClick={() => void decide(approval, "rejected")}>Deny</Button></div></Card>) : <p>No pending approval.</p>}
          </div>
          <AttachHelper session={selected} />
          <div>
            <div className={styles.transcriptHeaderRow}>
              <strong>Conversation log</strong>
              <label className={styles.liveTailToggle}>
                <input type="checkbox" checked={liveTail} onChange={(event) => setLiveTail(event.target.checked)} />
                Live tail
              </label>
            </div>
            <p className={styles.mutedSub}>
              Messages / tool calls for this Claude (or other provider) session.
            </p>
            {transcriptLoading ? <Loading label="Loading conversation…" /> : null}
            {transcriptError ? <ErrorState message={transcriptError} onRetry={() => setSelectedId(selected.id)} /> : null}
            {!transcriptLoading && !transcriptError && !transcript.length ? (
              <p className={styles.mutedText}>No transcript entries recorded for this session yet. Provider output is filled when the session completes or streams events.</p>
            ) : null}
            {!transcriptLoading && transcript.length > 0 ? (
              <div
                className={styles.transcriptContainer}
                role="log"
                aria-label="Session conversation transcript"
              >
                {transcript.slice(-80).map((entry, index) => (
                  <div key={`${entry.ts}-${index}`} className={styles.transcriptEntry}>
                    <div className={styles.transcriptMeta}>
                      {[entry.ts ? new Date(entry.ts).toLocaleTimeString() : "", entry.actor, entry.type, entry.toolName]
                        .filter(Boolean)
                        .join(" · ")}
                    </div>
                    <div className={styles.transcriptContent}>{entry.summary || entry.filePath || "—"}</div>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        </div> : null}
      </Dialog>

    </Page>
  );
}
