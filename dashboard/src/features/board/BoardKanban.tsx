"use client";

import Link from "next/link";
import { type MouseEvent as ReactMouseEvent, type ReactNode, useEffect, useMemo, useState } from "react";
import { Avatar, Badge, Pill } from "@/design";
import type { BadgeTone } from "@/design";
import { collabApi } from "@/features/collab/client";
import type { Agent, LiveBoardTask, TaskCategory } from "@/features/collab/types";
import {
  columnFor,
  isSameLocalDay,
  swarmPhase,
  swarmPhasePresentation,
  RESUMABLE_TASK_STATUSES,
  VISION_COLUMNS,
  type PhaseOverlay,
  type SwarmColumnId,
} from "@/features/swarm/columns";
import { cancelSwarmRun, unstoppedCancelIds } from "@/features/swarm/client";
import { groupSwarmRuns, type SwarmRunGroup } from "@/features/swarm/runAggregate";
import { SwarmRunCard } from "@/features/swarm/SwarmRunCard";
import { SwarmRunModal } from "@/features/swarm/SwarmRunModal";
import type { SwarmBoardTask, SwarmRunDetail } from "@/features/swarm/types";
import { employeeName } from "@/features/team/types";
import { taskStaleness } from "./staleness";
import styles from "./board.module.css";

/**
 * THE kanban — the single column renderer for board cards, shared by /board and
 * the home cockpit's expandable board.
 *
 * Shape (the prototype's): ten fixed-width 300px columns in a horizontal
 * scroller, each with its own vertical scroll and its own render cap. The old
 * board stretched the same ten columns across the viewport, so no card was wide
 * enough to read a title in AND the board still did not fit; and it mounted
 * every card in every column, so a 600-card Blocked column mounted 600 card
 * subtrees nobody ever scrolled to.
 *
 * Card face: tags · title · 3-line-clamped description · compact meta ·
 * agent + up to three hover-revealed action chips (anything past the third
 * lives behind "More", so no per-card operation was lost). Cards always link to
 * /activity/{id}?kind=board; `onOpenTask` swaps that for the board's drawer.
 */

/** Cards rendered per column before "Show N more". */
export const COLUMN_RENDER_CAP = 12;

export function priorityTone(priority: string): "neutral" | "warn" | "danger" {
  return priority === "urgent" ? "danger" : priority === "high" ? "warn" : "neutral";
}

export function agentLabel(task: LiveBoardTask, agents: Agent[]): string | null {
  if (task.claimed_by) {
    const claimant = agents.find((a) => a.id === task.claimed_by);
    if (claimant) return claimant.name;
  }
  if (task.run_agent) return task.run_agent;
  return null;
}

// Run-state strings (queued|running|completed|failed|…) are themselves valid
// Badge tones, so the badge colours track the runner's real state.
export function runStateTone(state: string | null): BadgeTone {
  if (!state) return "neutral";
  if (state === "awaiting_approval") return "awaiting_approval";
  if (["running", "in_progress"].includes(state)) return "running";
  if (["completed", "done"].includes(state)) return "completed";
  if (["failed", "error", "blocked"].includes(state)) return "danger";
  if (["queued", "pending", "open", "claimed"].includes(state)) return "queued";
  if (["cancelled", "canceled"].includes(state)) return "cancelled";
  return "neutral";
}

/** Cards a Pause action applies to (live or about-to-be-live work). */
export function canPauseTask(task: LiveBoardTask): boolean {
  return (
    !task.archived &&
    ["open", "claimed", "in_progress", "awaiting_approval"].includes(task.status)
  );
}

/** Cards a Resume action applies to (paused/blocked work; retry resumes). */
export function canResumeTask(task: LiveBoardTask): boolean {
  return !task.archived && RESUMABLE_TASK_STATUSES.has(task.status);
}

/**
 * The one-line "why did this stop" hint. Server contract: `blocked_reason` is
 * only ever set on a blocked card and only ever names a FAILURE, so it needs no
 * hedging. `run_error` is the pre-contract fallback for control planes that do
 * not ship the field yet.
 */
export function blockedHint(task: LiveBoardTask): { short: string; full: string } | null {
  if (task.status !== "blocked") return null;
  const reason = task.blocked_reason ?? null;
  if (reason) {
    const label = reason.reason.replace(/_/g, " ");
    const detail = reason.detail?.trim();
    return {
      short: detail ? `${label} — ${detail}` : label,
      full: [detail ? `${label} — ${detail}` : label, reason.source ? `(${reason.source})` : ""]
        .filter(Boolean)
        .join(" "),
    };
  }
  const error = task.run_error?.trim();
  return error ? { short: error, full: error } : null;
}

type RunDetailState = "loading" | "ready" | "unavailable";

/** One per-card operation, in the order it earns a chip. */
interface CardAction {
  key: string;
  label: string;
  ariaLabel: string;
  title: string;
  disabled: boolean;
  primary?: boolean;
  run: (event: ReactMouseEvent) => void;
}

function latestMemberUpdate(group: SwarmRunGroup): string | null {
  let latest: { value: string; time: number } | null = null;
  for (const task of [group.root, ...group.members]) {
    if (!task) continue;
    const time = Date.parse(task.updated_at);
    if (Number.isFinite(time) && (!latest || time > latest.time)) {
      latest = { value: task.updated_at, time };
    }
  }
  return latest?.value ?? null;
}

export interface BoardKanbanProps {
  tasks: LiveBoardTask[];
  agents?: Agent[];
  categoryMap?: Record<string, TaskCategory>;
  phaseOverlay?: PhaseOverlay;
  showArchived?: boolean;
  /** Selection (bulk archive) — board page only. */
  selectionMode?: boolean;
  selectedIds?: string[];
  onToggleSelection?: (taskId: string) => void;
  selectionBusy?: boolean;
  /** Per-card actions; omit a callback to hide its chip. */
  onOpenFiles?: (taskId: string) => void;
  /** Card click opens the everything drawer (board page). Omit to keep the
   * default /activity/{id} navigation (home cockpit). */
  onOpenTask?: (taskId: string) => void;
  onArchive?: (taskId: string, event: ReactMouseEvent) => void;
  archivingId?: string | null;
  onPause?: (taskId: string, event: ReactMouseEvent) => void;
  onResume?: (taskId: string, event: ReactMouseEvent) => void;
  pausingId?: string | null;
  /** Decide the approval parking this card. Omit to hide Approve. */
  onApprove?: (approvalId: string, taskId: string, event: ReactMouseEvent) => void;
  approvingId?: string | null;
  /** Open a real terminal at the card's workspace -- the escape hatch for
   * sessions whose output does not stream to the browser. */
  onOpenTerminal?: (taskId: string, event: ReactMouseEvent) => void;
  terminalOpeningId?: string | null;
  /** Swarm badge click — board page filters in place; other surfaces navigate. */
  onSwarmBadge?: (runId: string) => void;
  activeRunFilter?: string;
  /** ONE card per swarm run (default). Off = legacy per-member cards (used
   * when a run filter narrows the board to a single run's members). */
  aggregateSwarms?: boolean;
  /** Called after a run-level pause/archive mutation so the host refreshes. */
  onRunMutated?: () => void;
  /**
   * Card ids the SERVER put in the Needs Response band
   * (GET /api/board/needs-response). It owns the strict predicate, so it wins
   * the Needs You column; `columnFor` (unchanged) stays the answer for every
   * other card and remains the SSE-interim fallback while the band catches up
   * with a card that just parked.
   */
  needsResponseIds?: ReadonlySet<string>;
  /** Cards rendered per column before "Show N more". */
  renderCap?: number;
}

export function BoardKanban({
  tasks,
  agents = [],
  categoryMap = {},
  phaseOverlay = new Map(),
  showArchived = false,
  selectionMode = false,
  selectedIds = [],
  onToggleSelection,
  selectionBusy = false,
  onOpenFiles,
  onOpenTask,
  onArchive,
  archivingId = null,
  onPause,
  onResume,
  pausingId = null,
  onApprove,
  approvingId = null,
  onOpenTerminal,
  terminalOpeningId = null,
  onSwarmBadge,
  activeRunFilter = "",
  aggregateSwarms = true,
  onRunMutated,
  needsResponseIds,
  renderCap = COLUMN_RENDER_CAP,
}: BoardKanbanProps) {
  const [openRun, setOpenRun] = useState<{
    group: SwarmRunGroup;
    detail: SwarmRunDetail | null;
    detailState: RunDetailState;
  } | null>(null);
  const [runBusy, setRunBusy] = useState(false);
  const [runNotice, setRunNotice] = useState<string | null>(null);
  /** How many cards each column currently renders (id → count). */
  const [shownPerColumn, setShownPerColumn] = useState<Record<string, number>>({});
  /** The card whose overflow ("More") menu is open — one at a time. */
  const [openMenuId, setOpenMenuId] = useState<string | null>(null);
  const countNow = new Date();

  // Any pointer outside the open menu, or Esc, closes it. Without this the menu
  // survives a click on the card underneath it and then floats over the board.
  useEffect(() => {
    if (!openMenuId) return;
    const close = () => setOpenMenuId(null);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        close();
      }
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [openMenuId]);

  // ONE card per swarm run: group members under their run and render a
  // SwarmRunCard in the run's aggregate column; solo cards stay task cards.
  const aggregation = useMemo(
    () => (aggregateSwarms ? groupSwarmRuns(tasks, phaseOverlay) : null),
    [aggregateSwarms, tasks, phaseOverlay],
  );

  /**
   * Partition into the ten derived columns ONCE, and derive the head counts
   * from that same partition — so a column's chip can never disagree with the
   * cards under it.
   */
  const partition = useMemo(() => {
    const empty = () => ({ runs: [] as SwarmRunGroup[], cards: [] as LiveBoardTask[] });
    const byColumn = new Map<SwarmColumnId, ReturnType<typeof empty>>(
      VISION_COLUMNS.map((column) => [column.id, empty()]),
    );
    const needsYou = (taskId: string | null | undefined) =>
      Boolean(taskId && needsResponseIds?.has(taskId));
    for (const group of aggregation?.groups ?? []) {
      const parked =
        needsYou(group.root?.id) || group.members.some((member) => needsYou(member.id));
      byColumn.get(parked ? "needs_you" : group.column)?.runs.push(group);
    }
    for (const task of aggregation ? aggregation.solo : tasks) {
      const column = needsYou(task.id)
        ? "needs_you"
        : columnFor(task as SwarmBoardTask, phaseOverlay);
      byColumn.get(column)?.cards.push(task);
    }
    return byColumn;
  }, [aggregation, needsResponseIds, phaseOverlay, tasks]);

  const runCategory = (group: SwarmRunGroup): TaskCategory | null => {
    const id = group.root?.category_id ?? group.members.find((task) => task.category_id)?.category_id;
    return id ? categoryMap[id] ?? null : null;
  };

  const pauseRun = async (group: SwarmRunGroup) => {
    setRunBusy(true);
    setRunNotice(null);
    try {
      const result = await cancelSwarmRun(group.runId);
      if (result === null) {
        setRunNotice("Cancel requested — result unknown; verify the run's sessions.");
      } else if (result.kill_complete === false) {
        const ids = unstoppedCancelIds(result);
        setRunNotice(
          `Partial cancel — not verifiably stopped: ${ids.length ? ids.join(", ") : (result.warning ?? "unknown")}. Press Pause again to re-check.`,
        );
      } else {
        setRunNotice("Run paused — live sessions stopped and verified.");
      }
      onRunMutated?.();
    } catch (reason) {
      setRunNotice(reason instanceof Error ? `Pause failed: ${reason.message}` : "Pause failed.");
    } finally {
      setRunBusy(false);
    }
  };

  const archiveRun = async (group: SwarmRunGroup) => {
    setRunBusy(true);
    setRunNotice(null);
    try {
      const ids = [group.root?.id, ...group.members.map((task) => task.id)].filter(
        (id): id is string => Boolean(id),
      );
      await collabApi.archiveTasks(ids);
      setRunNotice("Run archived.");
      setOpenRun(null);
      onRunMutated?.();
    } catch (reason) {
      setRunNotice(reason instanceof Error ? `Archive failed: ${reason.message}` : "Archive failed.");
    } finally {
      setRunBusy(false);
    }
  };

  /** The per-card operations that apply, in priority order. */
  const actionsFor = (task: LiveBoardTask): CardAction[] => {
    const actions: CardAction[] = [];
    const approval = task.pending_approval ?? null;
    const work = task.work ?? null;
    const filesCount = work?.files_count && work.files_count > 0 ? work.files_count : null;
    if (approval && onApprove) {
      actions.push({
        key: "approve",
        label: approvingId === approval.id ? "Approving…" : "Approve",
        ariaLabel: `Approve ${approval.command.slice(0, 60)} for ${task.title}`,
        title: approval.command,
        disabled: approvingId === approval.id,
        primary: true,
        run: (event) => onApprove(approval.id, task.id, event),
      });
    }
    if (onPause && canPauseTask(task)) {
      actions.push({
        key: "pause",
        label: pausingId === task.id ? "Pausing…" : "Pause",
        ariaLabel: `Pause ${task.title}`,
        title: "Pause this card's live work",
        disabled: pausingId === task.id,
        run: (event) => onPause(task.id, event),
      });
    }
    if (onResume && canResumeTask(task)) {
      actions.push({
        key: "resume",
        label: pausingId === task.id ? "Resuming…" : "Resume",
        ariaLabel: `Resume ${task.title}`,
        title: "Resume this card",
        disabled: pausingId === task.id,
        run: (event) => onResume(task.id, event),
      });
    }
    // Terminal reveal needs a real workspace on disk; session-backed and
    // run-backed cards have one, plan-only cards do not.
    if (onOpenTerminal && Boolean(task.result_ref || task.run_id)) {
      actions.push({
        key: "terminal",
        label: terminalOpeningId === task.id ? "Opening…" : "Terminal",
        ariaLabel: `Open terminal for ${task.title}`,
        title: "Open a terminal at this task's workspace",
        disabled: terminalOpeningId === task.id,
        run: (event) => onOpenTerminal(task.id, event),
      });
    }
    if (onOpenFiles) {
      const done = task.status === "done";
      actions.push({
        key: "files",
        label: `${done ? "Deliverables" : "Files"}${filesCount ? ` · ${filesCount}` : ""}`,
        ariaLabel: done ? `View deliverables for ${task.title}` : `Open files for ${task.title}`,
        title: done ? "View what this card produced" : "Files in this card's workspace",
        disabled: false,
        primary: done,
        run: () => onOpenFiles(task.id),
      });
    }
    if (onArchive && !showArchived && !task.archived) {
      const label =
        task.status === "in_progress" || (task.status === "open" && task.run_id)
          ? "Archive (stops work)"
          : "Archive";
      actions.push({
        key: "archive",
        label: archivingId === task.id ? "Archiving…" : "Archive",
        ariaLabel: label,
        title: label,
        disabled: archivingId === task.id || selectionBusy,
        run: (event) => onArchive(task.id, event),
      });
    }
    return actions;
  };

  const renderActions = (task: LiveBoardTask): ReactNode => {
    const actions = actionsFor(task);
    if (!actions.length) return null;
    // Three chips max. With more than three, the third becomes "More" so the
    // overflow is still one click away rather than gone.
    const inline = actions.length > 3 ? actions.slice(0, 2) : actions;
    const overflow = actions.slice(inline.length);
    const chip = (action: CardAction, inMenu = false) => (
      <button
        key={action.key}
        type="button"
        role={inMenu ? "menuitem" : undefined}
        className={`${styles.kact} ${action.primary ? styles.kactPrimary : ""}`}
        title={action.title}
        aria-label={action.ariaLabel}
        disabled={action.disabled}
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          setOpenMenuId(null);
          action.run(event);
        }}
      >
        {action.label}
      </button>
    );
    return (
      <div className={styles.kcardActions}>
        {inline.map((action) => chip(action))}
        {overflow.length ? (
          <span
            className={styles.kactMenuWrap}
            onPointerDown={(event) => event.stopPropagation()}
          >
            <button
              type="button"
              className={styles.kact}
              aria-haspopup="menu"
              aria-expanded={openMenuId === task.id}
              aria-label={`More actions for ${task.title}`}
              title="More actions"
              onClick={(event) => {
                event.preventDefault();
                event.stopPropagation();
                setOpenMenuId((current) => (current === task.id ? null : task.id));
              }}
            >
              More
            </button>
            {openMenuId === task.id ? (
              <span className={styles.kactMenu} role="menu" aria-label={`More actions for ${task.title}`}>
                {overflow.map((action) => chip(action, true))}
              </span>
            ) : null}
          </span>
        ) : null}
      </div>
    );
  };

  return (
    <>
      {/* Outside the scroller: inside it the notice becomes a zero-width flex
          item wedged between two columns. */}
      {runNotice ? <p className={styles.archiveNotice} role="status">{runNotice}</p> : null}
      <section className={styles.kanban} aria-label="Live task board">
      {VISION_COLUMNS.map((column) => {
        const { runs: columnRuns, cards: columnTasks } = partition.get(column.id) ?? {
          runs: [],
          cards: [],
        };
        const total = columnRuns.length + columnTasks.length;
        const completedToday =
          column.id === "completed"
            ? columnTasks.filter(
                (task) => task.status === "done" && isSameLocalDay(task.updated_at, countNow),
              ).length +
              columnRuns.filter((group) => {
                const updatedAt = latestMemberUpdate(group);
                return updatedAt ? isSameLocalDay(updatedAt, countNow) : false;
              }).length
            : 0;
        const shown = shownPerColumn[column.id] ?? renderCap;
        const shownRuns = columnRuns.slice(0, shown);
        const shownTasks = columnTasks.slice(0, Math.max(0, shown - shownRuns.length));
        const hidden = total - shownRuns.length - shownTasks.length;
        const nextPage = Math.min(renderCap, hidden);
        return (
          <section
            key={column.id}
            className={styles.kcol}
            style={{ "--column-tone": column.tone } as React.CSSProperties}
            aria-labelledby={`column-${column.id}`}
          >
            <div className={styles.kcolHead}>
              <span className={styles.kdot} aria-hidden="true" />
              <h3 id={`column-${column.id}`} className={styles.kname}>{column.label}</h3>
              {column.id === "completed" && completedToday > 0 ? (
                <span className={styles.kcountToday} aria-hidden="true">{completedToday} today</span>
              ) : null}
              <span className={styles.kcount} aria-hidden="true">{total}</span>
              <span className={styles.srOnly}>
                {total} card{total === 1 ? "" : "s"} in {column.label}
                {column.id === "completed" ? `; ${completedToday} completed today` : ""}
              </span>
            </div>
            <div className={styles.kcolBody}>
              {shownRuns.map((group) => (
                <SwarmRunCard
                  key={group.runId}
                  group={group}
                  category={runCategory(group)}
                  onOpen={(g, detail) => setOpenRun({
                    group: g,
                    detail,
                    detailState: detail ? "ready" : "loading",
                  })}
                  onOpenUpdate={(g, detail) => setOpenRun((current) =>
                    current?.group.runId === g.runId
                      ? { ...current, detail, detailState: "ready" }
                      : current,
                  )}
                  onOpenUnavailable={(g) => setOpenRun((current) =>
                    current?.group.runId === g.runId
                      ? { ...current, detailState: "unavailable" }
                      : current,
                  )}
                  onPause={(g) => void pauseRun(g)}
                  pausing={runBusy}
                />
              ))}
              {shownTasks.map((task) => {
                const who = agentLabel(task, agents);
                const work = task.work ?? null;
                const executionAgent = task.run_agent ?? work?.agent ?? who;
                const progress = work && Number.isFinite(work.steps_total) ? work : task.run_progress;
                const status = work?.state ?? task.status;
                const currentStep = work?.current_step;
                const isActive = status === "in_progress" || status === "running";
                const category = task.category_id ? categoryMap[task.category_id] : null;
                const swarmRunId = (task as SwarmBoardTask).swarm_run_id ?? null;
                const approval = task.pending_approval ?? null;
                const phase = ["claimed", "in_progress"].includes(task.status)
                  ? swarmPhasePresentation(swarmPhase(task, phaseOverlay))
                  : null;
                const staleness = taskStaleness(task.updated_at);
                const blocked = blockedHint(task);
                const steps = progress && progress.steps_total > 0 ? progress : null;
                const cost = typeof work?.cost_usd === "number" ? work.cost_usd : null;
                return (
                  <article
                    key={task.id}
                    className={styles.kcard}
                    style={{ "--card-tone": column.tone } as React.CSSProperties}
                  >
                    {/* Stretched-link overlay: a SIBLING of the card's interactive
                        children, never their ancestor — interactive content must
                        not nest inside an <a>. Children that take clicks sit
                        above it (see the z-index raise in board.module.css). */}
                    <Link
                      href={`/activity/${encodeURIComponent(task.id)}?kind=board`}
                      className={styles.kcardLink}
                      aria-label={`Open details for ${task.title}`}
                      onClick={onOpenTask ? (event) => { event.preventDefault(); onOpenTask(task.id); } : undefined}
                    />
                      {!showArchived && selectionMode && onToggleSelection ? (
                        <label
                          className={styles.kcardSelect}
                          onClick={(event) => { event.stopPropagation(); }}
                        >
                          <input
                            type="checkbox"
                            checked={selectedIds.includes(task.id)}
                            onChange={() => onToggleSelection(task.id)}
                            aria-label={`Select ${task.title}`}
                            disabled={selectionBusy}
                          />
                          <span>Select</span>
                        </label>
                      ) : null}
                      <div className={styles.kcardTags}>
                        <Badge tone={priorityTone(task.priority)}>{task.priority}</Badge>
                        {task.company_slug ? (
                          <Badge tone="neutral" title="Company">{task.company_slug}</Badge>
                        ) : null}
                        {task.owner_employee_id ? (
                          <Badge tone="neutral" title={`Owner: ${employeeName(task.owner_employee_id)}`}>
                            {employeeName(task.owner_employee_id)}
                          </Badge>
                        ) : null}
                        {task.discipline ? <Badge tone="neutral">{task.discipline}</Badge> : null}
                        {category ? (
                          <Pill
                            tone="neutral"
                            className={category.color ? styles.categoryPill : undefined}
                            style={category.color ? ({ "--cat-color": category.color } as React.CSSProperties) : undefined}
                          >
                            {category.name}
                          </Pill>
                        ) : null}
                        {swarmRunId && onSwarmBadge ? (
                          <Badge
                            tone="promote"
                            role="button"
                            tabIndex={0}
                            title="Filter the board to this swarm run"
                            aria-label={`Filter board to swarm run for ${task.title}`}
                            aria-pressed={activeRunFilter === swarmRunId}
                            className={styles.clickableBadge}
                            onClick={(event) => {
                              event.preventDefault();
                              event.stopPropagation();
                              onSwarmBadge(swarmRunId);
                            }}
                            onKeyDown={(event) => {
                              if (event.key === "Enter" || event.key === " ") {
                                event.preventDefault();
                                event.stopPropagation();
                                onSwarmBadge(swarmRunId);
                              }
                            }}
                          >
                            Swarm
                          </Badge>
                        ) : null}
                        {phase ? (
                          <Badge tone={phase.tone} title={`Swarm phase: ${phase.label}`}>{phase.label}</Badge>
                        ) : null}
                        {task.archived ? <Badge tone="neutral">Archived</Badge> : null}
                        <Badge
                          tone={runStateTone(status)}
                          category="runState"
                          className={styles.kcardStatus}
                          title={status.replace(/_/g, " ")}
                        >
                          {status.replace(/_/g, " ")}
                        </Badge>
                      </div>
                      <h4 className={styles.kcardTitle}>{task.title}</h4>
                      {task.description ? <p className={styles.kcardDesc}>{task.description}</p> : null}
                      {blocked ? (
                        <p className={styles.kcardBlocked} title={blocked.full}>
                          <span aria-hidden="true">⚠</span>
                          <span className={styles.kcardBlockedText}>{blocked.short}</span>
                        </p>
                      ) : null}
                      {approval ? (
                        <div className={styles.kcardApproval}>
                          <span className={styles.kcardApprovalLabel}>
                            Waiting on you{approval.action_class ? ` · ${approval.action_class.replace(/_/g, " ")}` : ""}
                          </span>
                          {/* The real command, not the tool name. `proposed_action`
                              is only ever "Bash", which nobody can decide on. */}
                          <code className={styles.kcardApprovalCommand} title={approval.command}>
                            {approval.command}
                          </code>
                        </div>
                      ) : null}
                      {steps || staleness.days >= 1 || cost !== null || currentStep ? (
                        <div className={styles.kcardMeta}>
                          {steps ? (
                            <span>{steps.steps_done}/{steps.steps_total} steps</span>
                          ) : null}
                          {staleness.days >= 1 ? (
                            <span className={staleness.stale ? styles.kcardStale : undefined}>
                              updated {staleness.days}d ago
                            </span>
                          ) : null}
                          {cost !== null ? <span>${cost.toFixed(2)}</span> : null}
                          {currentStep ? (
                            <span className={styles.kcardStep} title={currentStep}>Now: {currentStep}</span>
                          ) : null}
                        </div>
                      ) : null}
                      <div className={styles.kcardFoot}>
                        {executionAgent ? (
                          <span className={styles.kcardAgent} title={`Agent: ${executionAgent}`}>
                            <Avatar name={executionAgent} size="sm" />
                            <span>{executionAgent}</span>
                          </span>
                        ) : null}
                        {isActive ? (
                          <span className={styles.kcardLive} aria-label="Work in progress">
                            <span className={styles.kcardSpinner} aria-hidden="true" />Live
                          </span>
                        ) : null}
                        {renderActions(task)}
                      </div>
                  </article>
                );
              })}
              {hidden > 0 ? (
                <button
                  type="button"
                  className={styles.kmore}
                  onClick={() =>
                    setShownPerColumn((current) => ({
                      ...current,
                      [column.id]: (current[column.id] ?? renderCap) + nextPage,
                    }))
                  }
                >
                  Show {nextPage} more
                </button>
              ) : null}
              {total === 0 ? (
                <p className={styles.kcolEmpty}>
                  {column.id === "needs_you" ? "Nothing waiting on you" : "No tasks"}
                </p>
              ) : null}
            </div>
          </section>
        );
      })}
      {openRun ? (
        <>
          {openRun.detailState !== "ready" ? (
            <p className={styles.runDetailState} role="status">
              {openRun.detailState === "loading" ? "loading run detail…" : "run detail unavailable"}
            </p>
          ) : null}
          <SwarmRunModal
            group={openRun.group}
            detail={openRun.detail}
            onClose={() => setOpenRun(null)}
            onPause={(g) => void pauseRun(g)}
            onArchive={(g) => void archiveRun(g)}
            busy={runBusy}
          />
        </>
      ) : null}
      </section>
    </>
  );
}
