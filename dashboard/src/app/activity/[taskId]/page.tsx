"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Button, Card, DefinitionList, EmptyState, ErrorState, Loading, Page, PageHeader, Section, StatusDot } from "@/design";
import { collabApi } from "@/features/collab/client";
import type { BoardTask, BoardTaskSessions, BoardTaskStatus, TaskTurn, LonghaulTaskDetail } from "@/features/collab/types";
import { TaskDetailPanel } from "@/features/board/TaskDetailPanel";
// AttemptTimeline is shared with the board drawer (§2.6) so the two never
// diverge — the legacy page delegates to it below.
import { AttemptTimeline as SharedAttemptTimeline } from "@/features/board/AttemptTimeline";
import boardStyles from "@/features/board/board.module.css";
import { api } from "@/lib/api";
import type { RunDetail, Session, TaskRow } from "@/lib/contracts";
import { extractAPIsUsed, extractFilesTouched, parseTranscript, type TranscriptEntry } from "@/lib/transcriptParser";
import { useEvents } from "@/lib/useEvents";
import { startVisibilityPoll } from "@/lib/pollWhenVisible";
import { MarkdownBody } from "@/features/galaxy/markdown";
import { formatPercent } from "@/lib/format";
import styles from "./activity.module.css";

type WorkKind = "orchestration" | "run" | "session" | "board";
type Stage = "queued" | "planning" | "running" | "reviewing" | "done" | "failed" | "cancelled";

const STAGES: Array<{ id: Stage; label: string }> = [
  { id: "queued", label: "Queued" },
  { id: "planning", label: "Planning" },
  { id: "running", label: "Running" },
  { id: "reviewing", label: "Reviewing" },
  { id: "done", label: "Done" },
];

function normalizedStage(value: string | undefined): Stage {
  if (value === "completed") return "done";
  if (value === "cancelled" || value === "killed") return "cancelled";
  if (value === "failed") return "failed";
  if (["validating", "reviewing", "revision_requested"].includes(value ?? "")) return "reviewing";
  if (["running", "resuming", "awaiting_approval"].includes(value ?? "")) return "running";
  if (["draft", "ready", "starting"].includes(value ?? "")) return "planning";
  return "queued";
}

function terminal(state: string | undefined) {
  return ["completed", "failed", "cancelled", "killed"].includes(state ?? "");
}

function todoProgressView(session: Session): { pct: number | undefined; primary: string; secondary: string } {
  const p = session.progress ?? { done: 0, total: 0, pct: 0 };
  if (p.total > 0) return { pct: p.pct, primary: `${p.done}/${p.total} tasks`, secondary: `${p.pct}%` };
  if (session.state === "completed") return { pct: 100, primary: "Done", secondary: "100%" };
  if (session.state === "failed") return { pct: 0, primary: "Failed", secondary: "Failed" };
  if (session.state === "cancelled") return { pct: 0, primary: "Cancelled", secondary: "Cancelled" };
  if (session.state === "killed") return { pct: 0, primary: "Killed", secondary: "Killed" };
  return { pct: undefined, primary: session.state === "starting" ? "Starting…" : "Working…", secondary: "—" };
}

function boardStateProxy(status: BoardTaskStatus): string {
  switch (status) {
    case "done": return "completed";
    case "cancelled": return "cancelled";
    case "blocked": return "revision_requested";
    case "in_progress": return "running";
    case "claimed": return "starting";
    default: return "queued";
  }
}

function elapsed(start: string | null | undefined, finish?: string | null): number | null {
  if (!start) return null;
  const ms = Date.parse(finish ?? new Date().toISOString()) - Date.parse(start);
  return Number.isFinite(ms) && ms >= 0 ? ms : null;
}

function durationLabel(milliseconds: number | null): string {
  if (milliseconds === null) return "—";
  const seconds = Math.max(0, Math.round(milliseconds / 1000));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function formatTime(value: string) {
  if (!value) return "Just now";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function progress(run: RunDetail | null, stage: Stage, session: Session | null): { percent: number | null; label: string } {
  const stageTerminal = stage === "done" || stage === "failed" || stage === "cancelled";
  if (!run?.steps.length) {
    if (stageTerminal) {
      const duration = session ? elapsed(session.created_at, session.last_activity_at) : null;
      const stateLabel = stage === "done" ? "Completed" : stage === "failed" ? "Failed" : "Cancelled";
      return { percent: stage === "done" ? 100 : 0, label: duration === null ? stateLabel : `${stateLabel} · ${durationLabel(duration)}` };
    }
    const total = elapsed(run?.started_at ?? run?.queued_at, run?.finished_at);
    return { percent: null, label: `Elapsed: ${durationLabel(total)} — Estimating…` };
  }
  const completed = run.steps.filter((step) => ["completed", "failed", "skipped", "compensated"].includes(step.status));
  const percent = Math.round((completed.length / run.steps.length) * 100);
  if (stageTerminal) return { percent: 100, label: `${run.state === "completed" ? "Completed" : "Finished"} · ${durationLabel(elapsed(run.started_at ?? run.queued_at, run.finished_at))}` };
  const durations = completed.map((step) => elapsed(step.started_at, step.finished_at)).filter((value): value is number => value !== null);
  const average = durations.length ? durations.reduce((sum, value) => sum + value, 0) / durations.length : null;
  const remaining = average === null ? null : average * (run.steps.length - completed.length);
  return { percent, label: remaining === null ? `${percent}% — Estimating…` : `${percent}% — ~${durationLabel(remaining)} remaining` };
}

function Stepper({ current }: { current: Stage }) {
  const visible = current === "failed" || current === "cancelled" ? [...STAGES.slice(0, 4), { id: current, label: current === "failed" ? "Failed" : "Cancelled" }] : STAGES;
  const index = visible.findIndex((stage) => stage.id === current);
  return <ol className={styles.stepper} aria-label="Task stage">
    {visible.map((stage, stageIndex) => {
      const isCurrent = stageIndex === index;
      const isPast = stageIndex < index;
      const itemClass = `${styles.stepperItem} ${isCurrent ? styles.stepperItemActive : isPast ? styles.stepperItemCompleted : styles.stepperItemPending}`;
      const badgeClass = `${styles.stepperBadge} ${stageIndex <= index ? styles.stepperBadgeActiveCompleted : styles.stepperBadgePending}`;
      return (
        <li key={stage.id} className={itemClass}>
          <span aria-hidden="true" className={badgeClass}>{stageIndex + 1}</span>
          {stage.label}
          {stageIndex < visible.length - 1 ? <span aria-hidden="true" className={styles.stepperDivider}>—</span> : null}
        </li>
      );
    })}
  </ol>;
}

// Conversation panel for task steering
function TaskConversationPanel({ taskId }: { taskId: string }) {
  const [messages, setMessages] = useState<TaskTurn[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const { lastEvent } = useEvents(["session.updated"]);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const turns = await collabApi.fetchTaskConversation(taskId, 50);
      setMessages(turns);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load conversation");
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (lastEvent?.type === "task.message" && lastEvent?.target_id === taskId) {
      void refresh();
    }
  }, [lastEvent, taskId, refresh]);

  const sendMessage = async () => {
    if (!draft.trim() || sending) return;
    setSending(true);
    try {
      await collabApi.sendTaskMessage(taskId, draft.trim());
      setDraft("");
      void refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not send message");
    } finally {
      setSending(false);
    }
  };

  return (
    <Card>
      {error ? <p className={styles.errorMessage}>{error}</p> : null}
      {loading && !messages.length ? (
        <Loading label="Loading conversation…" />
      ) : messages.length ? (
        <div className={styles.chatLog}>
          {messages.map((m) => (
            <div key={m.id} className={styles.chatItem}>
              <div className={styles.chatMeta}>
                <Badge tone="neutral">{m.role}</Badge>
                <span className={styles.chatMetaTime}>{formatTime(m.created_at)}</span>
              </div>
              <p className={styles.chatText}>{m.content}</p>
            </div>
          ))}
        </div>
      ) : (
        <EmptyState message="No messages yet. Send a message to start steering this task." />
      )}

      <div className={styles.guidanceContainer}>
        <textarea
          placeholder="Send a message to this task…"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          disabled={sending}
          rows={3}
          className={styles.textarea}
        />
        <Button onClick={() => void sendMessage()} disabled={!draft.trim() || sending}>
          {sending ? "Sending…" : "Send message"}
        </Button>
      </div>
    </Card>
  );
}

// Workbook viewer
function WorkbookViewer({ workbookContent }: { workbookContent: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <Card>
      <div className={styles.textNotice}>
        <Button variant="secondary" size="sm" onClick={() => setExpanded(!expanded)}>
          {expanded ? "Hide workbook" : "Show workbook"}
        </Button>
      </div>
      {expanded && workbookContent ? (
        <div className={styles.workbookCode}>
          <MarkdownBody markdown={workbookContent} resolveWikilink={() => null} />
        </div>
      ) : workbookContent ? (
        <p className={styles.workbookFaint}>Workbook has {workbookContent.split("\n").length} lines of content</p>
      ) : (
        <EmptyState message="No workbook content available" />
      )}
    </Card>
  );
}

// Attempt chain timeline — the legacy page delegates to the shared component
// (imported at the top) so the drawer and this page never diverge.
function AttemptTimeline({ longhaulDetail }: { longhaulDetail: LonghaulTaskDetail }) {
  return <SharedAttemptTimeline attempts={longhaulDetail.attempts ?? []} />;
}

// Chat v2 §2.6: board cards render the shared everything panel full-page —
// the SAME components the board's TaskDetailDrawer mounts.
function BoardTaskDetailPage({ taskId }: { taskId: string }) {
  return (
    <Page>
      <PageHeader
        before={
          <Link href="/board" className={boardStyles.backLink}>
            ← Board
          </Link>
        }
        eyebrow="Task detail"
        title="Task"
      />
      <TaskDetailPanel taskId={taskId} />
    </Page>
  );
}

export default function ActivityDetailPage() {
  const params = useParams<{ taskId: string | string[] }>();
  const searchParams = useSearchParams();
  const kindParam = searchParams.get("kind");
  const taskId = typeof params.taskId === "string" ? params.taskId : params.taskId?.[0] ?? "";
  if (kindParam === "board") {
    return <BoardTaskDetailPage taskId={taskId} />;
  }
  return <LegacyActivityDetail />;
}

function LegacyActivityDetail() {
  const params = useParams<{ taskId: string | string[] }>();
  const searchParams = useSearchParams();
  const id = typeof params.taskId === "string" ? params.taskId : params.taskId?.[0] ?? "";
  const kindParam = searchParams.get("kind");
  const kind: WorkKind = kindParam === "run" || kindParam === "session" || kindParam === "board" ? kindParam : "orchestration";
  const [task, setTask] = useState<TaskRow | null>(null);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [boardTask, setBoardTask] = useState<BoardTask | null>(null);
  const [longhaulDetail, setLonghaulDetail] = useState<LonghaulTaskDetail | null>(null);
  const [taskSessions, setTaskSessions] = useState<BoardTaskSessions | null>(null);
  const [entries, setEntries] = useState<TranscriptEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [guidance, setGuidance] = useState("");
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [archiving, setArchiving] = useState(false);
  const [archiveNotice, setArchiveNotice] = useState<string | null>(null);
  // board.updated / task.message / swarm.event are what an ACTIVE board card
  // actually emits — without them the page only refreshed on session events
  // it could not even resolve.
  const { lastEvent, connected, error: streamError } = useEvents([
    "run.updated",
    "step.updated",
    "session.updated",
    "board.updated",
    "task.message",
    "swarm.event",
  ]);

  const refresh = useCallback(async () => {
    if (!id) { setError("No activity id was provided."); setLoading(false); return; }
    setLoading(true);
    try {
      let nextTask: TaskRow | null = null;
      let nextRun: RunDetail | null = null;
      let nextSession: Session | null = null;
      let nextBoardTask: BoardTask | null = null;
      let nextLonghaul: LonghaulTaskDetail | null = null;
      let nextTaskSessions: BoardTaskSessions | null = null;
      if (kind === "session") {
        nextSession = await api.session(id);
      } else if (kind === "run") {
        nextRun = await api.run(id);
      } else if (kind === "board") {
        const activeBoard = await collabApi.liveBoard();
        nextBoardTask = activeBoard.find((candidate) => candidate.id === id) ?? null;
        if (!nextBoardTask) {
          const archivedBoard = await collabApi.liveBoard({ archived: true }).catch(() => []);
          nextBoardTask = archivedBoard.find((candidate) => candidate.id === id) ?? null;
        }
        if (!nextBoardTask) throw new Error("This board task was not found.");

        // Fetch longhaul detail if it's a longhaul task
        if (nextBoardTask.lane === "longhaul") {
          try {
            nextLonghaul = await collabApi.fetchLonghaulDetail(id);
          } catch (reason) {
            console.debug("Longhaul detail not available:", reason);
          }
        }

        // Unified session lookup — an ACTIVE card's result_ref is orch_/run_/
        // null until DONE, so the ses_ fallback below never fires for live
        // work; this endpoint resolves the real sessions across every lane.
        try {
          nextTaskSessions = await collabApi.fetchTaskSessions(id);
          const liveId = nextTaskSessions.live_session_id
            ?? nextTaskSessions.sessions[nextTaskSessions.sessions.length - 1]?.id
            ?? null;
          if (liveId) nextSession = await api.session(liveId).catch(() => null);
        } catch (reason) {
          console.debug("Task sessions not available:", reason);
        }
      } else {
        const [tasks, runs] = await Promise.all([api.tasks(), api.runs()]);
        nextTask = tasks.find((candidate) => candidate.id === id) ?? null;
        const latestRun = runs.filter((candidate) => candidate.task_id === id).sort((a, b) => Date.parse(b.queued_at) - Date.parse(a.queued_at))[0];
        if (latestRun) nextRun = await api.run(latestRun.id);
        if (!nextTask && !nextRun) throw new Error("This activity item was not found.");
      }
      if (nextRun?.session_ref) {
        const sessions = await api.sessions();
        const matched = sessions.find((candidate) => candidate.id === nextRun?.session_ref || candidate.session_ref === nextRun?.session_ref) ?? null;
        nextSession = matched ? await api.session(matched.id).catch(() => matched) : null;
      }
      if (!nextSession && nextBoardTask?.result_ref?.startsWith("ses_")) {
        nextSession = await api.session(nextBoardTask.result_ref).catch(() => null);
      }
      const transcript = nextSession ? await api.sessionTranscript(nextSession.id).catch(() => []) : (nextRun?.events ?? []);
      setTask(nextTask);
      setRun(nextRun);
      setSession(nextSession);
      setBoardTask(nextBoardTask);
      setLonghaulDetail(nextLonghaul);
      setTaskSessions(nextTaskSessions);
      setEntries(parseTranscript(transcript));
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to load this activity item.");
    } finally {
      setLoading(false);
    }
  }, [id, kind]);

  useEffect(() => {
    void refresh();
    return startVisibilityPoll(() => void refresh(), 30_000);
  }, [refresh]);
  useEffect(() => { if (lastEvent) void refresh(); }, [lastEvent, refresh]);

  const rawState = session?.state ?? run?.state ?? task?.state ?? (boardTask ? boardStateProxy(boardTask.status) : undefined);
  const stage = normalizedStage(rawState);
  const isTerminal = terminal(rawState);
  const activity = useMemo(() => [...entries].sort((a, b) => Date.parse(b.ts || "0") - Date.parse(a.ts || "0")).slice(0, 20), [entries]);
  const files = useMemo(() => extractFilesTouched(entries), [entries]);
  const apis = useMemo(() => extractAPIsUsed(entries), [entries]);
  const sessionFiles = session?.files;
  const filesTouched = sessionFiles && sessionFiles.length ? sessionFiles : files;
  const progressInfo = progress(run, stage, session);
  const todos = session?.todos ?? [];
  const todoBar = session ? todoProgressView(session) : null;
  const title = session?.title ?? boardTask?.title ?? task?.title ?? (run ? `Run ${run.id}` : id || "Activity");
  const canSteer = Boolean(session && ["starting", "running", "awaiting_approval", "resuming"].includes(session.state));
  const canArchiveBoardTask = Boolean(boardTask && !boardTask.archived && ["done", "cancelled", "blocked"].includes(boardTask.status));
  const canPauseBoardTask = Boolean(boardTask && !boardTask.archived && ["open", "claimed", "in_progress"].includes(boardTask.status));
  const canResumeBoardTask = Boolean(boardTask && !boardTask.archived && ["cancelled", "blocked"].includes(boardTask.status));
  const isLonghaulTask = Boolean(boardTask?.lane === "longhaul");

  const sendGuidance = async () => {
    if (!session || !guidance.trim() || !canSteer) return;
    setSending(true);
    setNotice(null);
    try {
      await api.steerSession(session.id, guidance.trim());
      setGuidance("");
      setNotice("Guidance queued for the session's next turn.");
    } catch (reason) {
      setNotice(reason instanceof Error ? `Guidance was not sent: ${reason.message}` : "Guidance was not sent.");
    } finally {
      setSending(false);
    }
  };

  const archiveBoardTask = async () => {
    if (!boardTask) return;
    setArchiving(true);
    setArchiveNotice(null);
    try {
      await collabApi.archiveTask(boardTask.id);
      setArchiveNotice("Archived.");
      await refresh();
    } catch (reason) {
      setArchiveNotice(reason instanceof Error ? `Archive failed: ${reason.message}` : "Archive failed.");
    } finally {
      setArchiving(false);
    }
  };

  const pauseBoardTask = async () => {
    if (!boardTask) return;
    setArchiving(true);
    setArchiveNotice(null);
    try {
      await collabApi.pauseTask(boardTask.id);
      setArchiveNotice("Paused. Resume any time with the Resume button.");
      await refresh();
    } catch (reason) {
      setArchiveNotice(reason instanceof Error ? `Pause failed: ${reason.message}` : "Pause failed.");
    } finally {
      setArchiving(false);
    }
  };

  const resumeBoardTask = async () => {
    if (!boardTask) return;
    setArchiving(true);
    setArchiveNotice(null);
    try {
      await collabApi.retryTask(boardTask.id);
      setArchiveNotice("Resuming…");
      await refresh();
    } catch (reason) {
      setArchiveNotice(reason instanceof Error ? `Resume failed: ${reason.message}` : "Resume failed.");
    } finally {
      setArchiving(false);
    }
  };

  // The /activity index page was retired in the dashboard prune; the Board is the
  // one work surface every activity detail belongs to, so both kinds go back there.
  const back = <Link href="/board" className={styles.backLink}>← Board</Link>;
  if (loading && !task && !run && !session && !boardTask) return <Page><PageHeader before={back} eyebrow="Activity" title="Loading…" /><Loading label="Loading task detail…" /></Page>;
  if (error && !task && !run && !session && !boardTask) return <Page><PageHeader before={back} eyebrow="Activity" title={id || "Unknown activity"} /><ErrorState message={error} onRetry={() => void refresh()} /></Page>;

  return <Page>
    <PageHeader before={back} eyebrow="Task detail" title={title} titleAdornment={<Badge tone={stage === "done" ? "completed" : stage === "failed" || stage === "cancelled" ? "failed" : stage === "running" ? "running" : "neutral"}>{stage.replace(/_/g, " ")}</Badge>} lead={`${kind} · ${id}`} actions={<Badge tone={connected ? "ok" : streamError ? "danger" : "neutral"}>{connected ? "Live updates connected" : streamError ? "Live updates failed" : "Live updates reconnecting…"}</Badge>} />
    {loading ? <Loading label="Refreshing task detail…" /> : null}
    {streamError ? <ErrorState message="Live event stream failed to connect. Showing last known task data; reconnecting in the background." /> : null}
    {error ? <ErrorState message={`Latest update failed: ${error}`} onRetry={() => void refresh()} /> : null}

    {boardTask ? (
      <Section title="Board task" actions={<span className={styles.headerActions}>
        {canPauseBoardTask ? <Button variant="secondary" size="sm" onClick={() => void pauseBoardTask()} disabled={archiving}>{archiving ? "Working…" : "Pause"}</Button> : null}
        {canResumeBoardTask ? <Button size="sm" onClick={() => void resumeBoardTask()} disabled={archiving}>{archiving ? "Working…" : "Resume"}</Button> : null}
        {canArchiveBoardTask ? <Button variant="secondary" size="sm" onClick={() => void archiveBoardTask()} disabled={archiving}>{archiving ? "Working…" : "Archive"}</Button> : null}
      </span>}>
        <Card>
          <div className={styles.badgeRow}>
            <Badge tone={boardTask.status === "done" ? "completed" : boardTask.status === "cancelled" ? "cancelled" : boardTask.status === "blocked" ? "warn" : boardTask.status === "in_progress" ? "running" : "neutral"}>{boardTask.status.replace(/_/g, " ")}</Badge>
            {boardTask.archived ? <Badge tone="neutral">Archived</Badge> : null}
            {boardTask.lane ? <Badge tone="neutral">{boardTask.lane}</Badge> : null}
            {boardTask.attempt_count ? <Badge tone="neutral">Attempts: {boardTask.attempt_count}</Badge> : null}
            <span className={styles.textPriority}>Priority: {boardTask.priority}</span>
          </div>
          {archiveNotice ? <p className={styles.textFaint}>{archiveNotice}</p> : null}
        </Card>
      </Section>
    ) : null}

    {taskSessions && taskSessions.sessions.length ? (
      <Section title="Models working on this task">
        <Card>
          <div className={styles.grid}>
            {taskSessions.sessions.map((s) => (
              <div key={s.id} className={styles.flexRow}>
                <StatusDot state={["starting", "running", "resuming"].includes(s.state) ? "running" : s.state === "completed" ? "completed" : ["failed", "killed"].includes(s.state) ? "failed" : "queued"} />
                <strong>{s.model ?? "?"}</strong>
                {s.provider ? <Badge tone="neutral">{s.provider}</Badge> : null}
                <Badge tone={["starting", "running", "resuming", "awaiting_approval"].includes(s.state) ? "running" : s.state === "completed" ? "completed" : "neutral"}>{s.state.replace(/_/g, " ")}</Badge>
                {s.state === "awaiting_approval" ? (
                  <Link href="/inbox" className={styles.linkApprovals}>
                    Approve at /inbox →
                  </Link>
                ) : null}
                {s.step_title ? <span className={styles.textMutedSm}>{s.step_seq != null ? `Step ${s.step_seq}: ` : ""}{s.step_title}</span> : null}
                {s.effort ? <Badge tone="neutral">effort: {s.effort}</Badge> : null}
                <Link href={`/activity/${s.id}?kind=session`} className={styles.linkSession}>Session →</Link>
              </div>
            ))}
          </div>
          {taskSessions.orchestration ? (
            <p className={styles.textFaintMargin}>
              Orchestration {taskSessions.orchestration.status}{taskSessions.orchestration.stage ? ` · ${taskSessions.orchestration.stage}` : ""}
            </p>
          ) : null}
        </Card>
      </Section>
    ) : null}

    {session || (kind === "board" && boardTask) ? (
      <Section title="Live checklist">
        {session && todoBar ? <>
          <Card>
            <div className={styles.progressLabel}><strong>{todoBar.primary}</strong><span>{todoBar.secondary}</span></div>
            <progress value={todoBar.pct} max={100} aria-label="Todo progress" className={styles.progressBar} />
            <p className={styles.textFaintMargin}>{session.stage ? `Now: ${session.stage}` : terminal(session.state) ? "Finished." : "No active step right now."}</p>
          </Card>
          <Card className={styles.cardMarginTop}>
            {todos.length ? (
              <ul className={styles.todoList}>
                {todos.map((todo, index) => (
                  <li
                    key={`${index}-${todo.content}`}
                    className={todo.status === "pending" ? `${styles.todoItem} ${styles.todoItemPending}` : styles.todoItem}
                  >
                    <StatusDot state={todo.status === "completed" ? "completed" : todo.status === "in_progress" ? "running" : "queued"} />
                    <span
                      className={
                        todo.status === "completed"
                          ? styles.todoContentCompleted
                          : todo.status === "in_progress"
                          ? styles.todoContentInProgress
                          : styles.todoContentNormal
                      }
                    >
                      {todo.content}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState message="No checklist was recorded for this task." />
            )}
          </Card>
        </> : <Card><EmptyState message={isTerminal ? "This task finished without a monitored session — no live checklist was recorded." : "This board task has not started a session yet. Progress will appear here once it does."} /></Card>}
      </Section>
    ) : null}

    {/* Longhaul-specific sections */}
    {isLonghaulTask && longhaulDetail ? (
      <>
        <Section title="Task steering">
          <TaskConversationPanel taskId={id} />
        </Section>

        {longhaulDetail.workbook_content ? (
          <Section title="Workbook">
            <WorkbookViewer workbookContent={longhaulDetail.workbook_content} />
          </Section>
        ) : null}

        <Section title="Session chain">
          <AttemptTimeline longhaulDetail={longhaulDetail} />
        </Section>
      </>
    ) : (
      // Non-longhaul steering box (for backwards compatibility)
      <Section title="Steer this session">
        <Card>
          <label htmlFor="session-guidance" className={styles.labelGuidance}>Send guidance to this session</label>
          <textarea id="session-guidance" value={guidance} onChange={(event) => setGuidance(event.target.value)} placeholder="e.g. prioritize the failing test before continuing" disabled={!canSteer || sending} rows={3} className={styles.steerTextarea} />
          <div className={styles.steerContainer}><Button onClick={() => void sendGuidance()} disabled={!canSteer || sending || !guidance.trim()}>{sending ? "Sending…" : "Send guidance"}</Button><span className={styles.textMutedSm}>{notice ?? (canSteer ? "Applied on the session's next turn." : session ? "This session is not currently running." : "Steering is available when this run has a linked session.")}</span></div>
        </Card>
      </Section>
    )}

    <Section title="Progress">
      <Card>
        <div className={styles.progressLabel}><strong>{progressInfo.label}</strong><span>{formatPercent(progressInfo.percent !== null ? progressInfo.percent / 100 : null)}</span></div>
        <progress value={progressInfo.percent ?? undefined} max={100} aria-label="Task progress" className={styles.progressBar} />
      </Card>
    </Section>

    <Section title="Current stage"><Card><Stepper current={stage} /></Card></Section>

    <section className={styles.sidebarGrid}>
      <Card><h2 className={styles.headingMd}>Task / run / session</h2><DefinitionList items={[
        { label: "Task", value: task?.id ?? run?.task_id ?? boardTask?.id ?? "—" }, { label: "Run", value: run?.id ?? "—" }, { label: "Session", value: session?.id ?? "No linked session" },
        { label: "Agent", value: session?.provider ?? run?.agent ?? run?.harness ?? "—" }, { label: "Model", value: session?.model ?? run?.model ?? "—" }, { label: "Cost", value: session?.cost_usd ?? run?.cost_usd ? `$${(session?.cost_usd ?? run?.cost_usd ?? 0).toFixed(2)}` : "—" },
      ]} /></Card>
      <Card><h2 className={styles.headingMd}>Files touched</h2>{filesTouched.length ? <ul className={styles.filesTouchedList}>{filesTouched.slice(0, 12).map((file) => <li key={file}><code>{file}</code></li>)}</ul> : <EmptyState message={isTerminal ? "No files were recorded for this task." : "No file activity recorded yet."} />}</Card>
      <Card><h2 className={styles.headingMd}>APIs / connectors used</h2>{apis.length ? <div className={styles.badgeContainer}>{apis.map((name) => <Badge key={name} tone="neutral">{name}</Badge>)}</div> : <EmptyState message="No tool calls recorded yet." />}</Card>
    </section>

    <Section title="Live activity">
      <Card aria-live="polite">{activity.length ? <div className={styles.activityGrid}>{activity.map((entry, index) => <div key={`${entry.ts}-${entry.summary}-${index}`} className={styles.activityItem}><div className={styles.activityItemMeta}><span>{formatTime(entry.ts)}</span><Badge tone="neutral">{entry.type}</Badge><span>{entry.actor}</span></div><p className={styles.activityItemText}>{entry.summary}</p></div>)}</div> : <EmptyState title="No activity recorded yet" message="New messages and tool calls will appear here while the task runs." />}</Card>
    </Section>
  </Page>;
}
