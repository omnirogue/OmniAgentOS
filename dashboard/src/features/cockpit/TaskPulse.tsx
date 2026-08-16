"use client";

import Link from "next/link";
import { type MouseEvent as ReactMouseEvent, useEffect, useMemo, useState } from "react";
import { Badge, Button, Card, EmptyState, ErrorState, Loading, Pill, StatusDot } from "@/design";
import { USE_FIXTURES } from "@/features/collab/fixtures";
import { collabApi } from "@/features/collab/client";
import { useAgents, useLiveBoard } from "@/features/collab/hooks";
import type { TaskCategory } from "@/features/collab/types";
import { BoardKanban, agentLabel, canPauseTask, canResumeTask, runStateTone } from "@/features/board/BoardKanban";
import { BoardProgress } from "@/features/board/BoardProgress";
import { NeedsResponseQueue } from "@/features/board/NeedsResponseQueue";
import { listAttentionRank } from "@/features/board/needsResponse";
import { summarizeBoard } from "@/features/board/summary";
import { api } from "@/lib/api";
import { useSwarmOverview } from "@/features/swarm/hooks";
import type { PhaseOverlay } from "@/features/swarm/columns";
import type { SwarmRunGroup } from "@/features/swarm/runAggregate";
import { cancelSwarmRun, unstoppedCancelIds } from "@/features/swarm/client";
import boardStyles from "@/features/collab/collab.module.css";
import styles from "./cockpit.module.css";

/**
 * The cockpit's work panel (replaces the old LiveWork mini-kanban, which was a
 * diverged 3-column copy with unclickable cards). What the user sees, in
 * order:
 *
 * 1. A summary strip — active / queued / blocked / done-today counts plus
 *    per-category chips — always visible.
 * 2. A compact task LIST (active first), every row clickable through to the
 *    task's detail page, with inline Pause / Resume / Archive.
 * 3. "Show board" — the full kanban, hidden by default, rendered by the SAME
 *    BoardKanban component the /board page uses. One kanban, two surfaces.
 *
 * EVERY count and every row here is a REQUESTED task — one row per swarm run,
 * one per solo card — never a sub-task. A swarm's planner-generated member
 * cards are implementation detail: they read as a "6/11 sub-tasks" rollup and
 * as an on-demand todolist under the row (Sub-tasks ▸), so this list stays an
 * answer to "what did I ask for and how far along is it?" rather than a dump of
 * planner output. The counts come from `summarizeBoard` — the same function
 * /board's strip uses, so the two surfaces can never disagree.
 */

const LIST_LIMIT = 8;

function isToday(value: string | null | undefined): boolean {
  if (!value) return false;
  const then = new Date(value);
  const now = new Date();
  return then.getFullYear() === now.getFullYear() && then.getMonth() === now.getMonth() && then.getDate() === now.getDate();
}

/**
 * A swarm run's member cards, on demand — the todolist behind ONE requested
 * task. Deliberately a disclosure rather than list rows of their own: sub-tasks
 * are planner output, and promoting them to top-level rows is what made the
 * cockpit read as a wall of work nobody remembered asking for.
 */
function SubtaskList({ group }: { group: SwarmRunGroup }) {
  return (
    <ul
      style={{ listStyle: "none", margin: "var(--space-2) 0 var(--space-1)", padding: "var(--space-2) 0 var(--space-2) calc(var(--space-3) + 10px)", display: "grid", gap: "var(--space-2)", borderLeft: "2px solid var(--border)", marginLeft: "5px" }}
      aria-label={`Sub-tasks of ${group.title}`}
    >
      {group.members.map((member) => {
        const running = ["claimed", "in_progress"].includes(member.status);
        const now = member.work?.current_step ?? null;
        return (
          <li key={member.id} style={{ display: "flex", alignItems: "flex-start", gap: "var(--space-2)", opacity: ["pending", "open"].includes(member.status) ? 0.65 : 1 }}>
            <StatusDot state={running ? "running" : member.status === "done" ? "completed" : ["blocked", "cancelled"].includes(member.status) ? "failed" : "queued"} />
            <span style={{ minWidth: 0, flex: 1 }}>
              <Link href={`/activity/${encodeURIComponent(member.id)}?kind=board`} style={{ color: "inherit", textDecoration: "none" }}>
                <span style={{ fontSize: "var(--font-size-sm)", fontWeight: running ? 600 : 400, textDecoration: member.status === "done" ? "line-through" : "none" }}>{member.title}</span>
              </Link>
              {now ? <span style={{ display: "block", color: "var(--text-muted)", fontSize: "var(--font-size-sm)" }}>Now: {now}</span> : null}
            </span>
            <Badge tone={running ? "running" : member.status === "done" ? "completed" : ["blocked", "cancelled"].includes(member.status) ? "danger" : "neutral"}>
              {member.status.replace(/_/g, " ")}
            </Badge>
          </li>
        );
      })}
      {!group.members.length ? (
        <li style={{ color: "var(--text-muted)", fontSize: "var(--font-size-sm)" }}>Planning — sub-tasks appear as the planner provisions them.</li>
      ) : null}
    </ul>
  );
}

export function TaskPulse() {
  const { tasks, loading, error, hasLoaded, refresh } = useLiveBoard();
  const { agents } = useAgents();
  const [expanded, setExpanded] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [categories, setCategories] = useState<TaskCategory[]>([]);
  const [openSubtasks, setOpenSubtasks] = useState<string | null>(null);
  const { overview } = useSwarmOverview();
  const phaseOverlay = useMemo<PhaseOverlay>(
    () => new Map((overview?.tasks ?? []).map((task) => [task.task_id, task.phase])),
    [overview],
  );

  useEffect(() => {
    void collabApi.fetchCategories().then(setCategories).catch(() => undefined);
  }, []);

  const categoryMap = useMemo(() => {
    const map: Record<string, TaskCategory> = {};
    categories.forEach((cat) => { map[cat.id] = cat; });
    return map;
  }, [categories]);

  // ONE row per swarm run in the list and counts: a run with 8 live member
  // tasks reads "1 active swarm", never 8 rows. summarizeBoard does the
  // grouping AND the tallying in one pass, so the header numbers and the rows
  // below them are derived from the same objects.
  const summary = useMemo(() => summarizeBoard(tasks, phaseOverlay), [tasks, phaseOverlay]);
  const soloTasks = summary.solo;
  const runGroups = summary.groups;

  // "Done today" stays a requested-task count: a completed swarm run is ONE
  // thing finished today, not eleven. A run is dated by its root card (its
  // members finish at staggered times and are not the unit being counted).
  const doneToday = useMemo(() => {
    const runs = runGroups.filter(
      (group) => group.column === "completed" && isToday(group.root?.updated_at ?? group.root?.created_at),
    ).length;
    const solo = soloTasks.filter(
      (task) => task.status === "done" && isToday(task.updated_at ?? task.created_at),
    ).length;
    return runs + solo;
  }, [runGroups, soloTasks]);

  // Category chips: NON-terminal requested tasks per area — a swarm run takes
  // the category of its root card (falling back to the first member that has
  // one), so an 11-task swarm reads "Utilities · 1", not "Utilities · 11".
  const categoryCounts = useMemo(() => {
    const counts = new Map<string, number>();
    const bump = (id: string | null | undefined) => {
      if (id) counts.set(id, (counts.get(id) ?? 0) + 1);
    };
    for (const group of runGroups) {
      if (["completed", "blocked"].includes(group.column)) continue;
      bump(group.root?.category_id ?? group.members.find((task) => task.category_id)?.category_id);
    }
    for (const task of soloTasks) {
      if (["done", "cancelled"].includes(task.status)) continue;
      bump(task.category_id);
    }
    return [...counts.entries()]
      .map(([id, count]) => ({ category: categoryMap[id], count }))
      .filter((entry): entry is { category: TaskCategory; count: number } => Boolean(entry.category))
      .sort((a, b) => b.count - a.count);
  }, [runGroups, soloTasks, categoryMap]);

  const listed = useMemo(() => {
    const open = soloTasks.filter((task) => task.status !== "done" || isToday(task.updated_at ?? task.created_at));
    // Needs Response (awaiting_approval) rises above in_progress — §2.
    // No sort control: listAttentionRank IS the product order.
    const sorted = [...open].sort((a, b) => listAttentionRank(a) - listAttentionRank(b));
    return showAll ? sorted : sorted.slice(0, Math.max(0, LIST_LIMIT - runGroups.length));
  }, [soloTasks, showAll, runGroups.length]);
  const listedTotal = useMemo(
    () => soloTasks.filter((task) => task.status !== "done" || isToday(task.updated_at ?? task.created_at)).length + runGroups.length,
    [soloTasks, runGroups.length],
  );

  const act = async (taskId: string, action: "pause" | "resume" | "archive", event: ReactMouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setBusyId(taskId);
    setNotice(null);
    try {
      if (action === "pause") await collabApi.pauseTask(taskId);
      else if (action === "resume") await collabApi.retryTask(taskId);
      else await collabApi.archiveTask(taskId);
      setNotice(action === "pause" ? "Paused — resume any time." : action === "resume" ? "Resuming…" : "Archived.");
      refresh();
    } catch (reason) {
      setNotice(reason instanceof Error ? `${action} failed: ${reason.message}` : `${action} failed.`);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section aria-label="Live work">
      <div className={styles.liveHead}>
        <div>
          <h2 className={styles.liveTitle}>Live work</h2>
          <p className={styles.liveSub}>
            {loading && !tasks.length
              ? "Checking what's running…"
              : !hasLoaded && error
                /* D-2 (LiveSim LS-004) audit find: the board has never loaded
                 * and this fetch failed -- "What you dispatch shows up here"
                 * reads as a calm, confirmed-empty cockpit and hides that the
                 * fetch is broken. The ErrorState below still explains why. */
                ? "Could not check what's running."
                : tasks.length
                  ? `${summary.needsResponse > 0 ? `${summary.needsResponse} need${summary.needsResponse === 1 ? "s" : ""} you · ` : ""}${summary.running} active · ${summary.queued} queued · ${summary.blocked} blocked · ${doneToday} done today${summary.runCount ? ` · ${summary.runCount} swarm${summary.runCount === 1 ? "" : "s"}` : ""}`
                  : "What you dispatch shows up here and moves as agents work it."}
          </p>
        </div>
        <span style={{ display: "inline-flex", gap: "var(--space-2)", alignItems: "center" }}>
          {!loading && !error && tasks.length ? (
            <Badge tone={USE_FIXTURES ? "warn" : "ok"}>{USE_FIXTURES ? "Dev fixtures" : "Live"}</Badge>
          ) : null}
          {tasks.length ? (
            <Button variant="secondary" size="sm" aria-expanded={expanded} onClick={() => setExpanded((current) => !current)}>
              {expanded ? "Hide board" : "Show board"}
            </Button>
          ) : null}
        </span>
      </div>

      {!loading && !error && tasks.length ? <BoardProgress summary={summary} /> : null}

      {categoryCounts.length ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-2)", margin: "0 0 var(--space-3)" }} aria-label="Active work by category">
          {categoryCounts.map(({ category, count }) => (
            <Pill key={category.id} tone="neutral" style={category.color ? { borderColor: category.color, color: category.color } : undefined}>
              {category.name} · {count}
            </Pill>
          ))}
        </div>
      ) : null}

      {notice ? <p className={boardStyles.archiveNotice} role="status">{notice}</p> : null}

      {/* §2: Needs Response rises — strict predicate, real command, Approve in place. */}
      {!error && tasks.length ? (
        <NeedsResponseQueue
          tasks={tasks}
          hideWhenEmpty
          approvingId={busyId}
          onApprove={async (approvalId, _taskId, event) => {
            event.preventDefault();
            event.stopPropagation();
            setBusyId(approvalId);
            setNotice(null);
            try {
              await api.decideApproval(approvalId, "approved");
              setNotice("Approved — the session resumes on its next poll.");
              refresh();
            } catch (reason) {
              setNotice(reason instanceof Error ? `Approve failed: ${reason.message}` : "Approve failed.");
            } finally {
              setBusyId(null);
            }
          }}
        />
      ) : null}

      {loading && !tasks.length ? <Loading variant="skeleton" label="Loading the board" lines={5} /> : null}
      {!loading && error ? <ErrorState message={error} onRetry={refresh} /> : null}
      {!loading && !error && tasks.length === 0 ? (
        <EmptyState
          title="Nothing running yet"
          message="Tell me what you want above. Once you confirm, it lands here and moves as agents pick it up."
        />
      ) : null}

      {!error && (listed.length > 0 || runGroups.length > 0) && !expanded ? (
        <Card padding="sm">
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid" }} aria-label="Tasks">
            {runGroups.map((group) => {
              const rootId = group.root?.id ?? null;
              const pct = group.total > 0 ? Math.round((group.done / group.total) * 100) : 0;
              const running = group.running > 0;
              const row = (
                <>
                  <StatusDot state={running ? "running" : group.column === "completed" ? "completed" : group.column === "blocked" ? "failed" : "queued"} />
                  <span style={{ flex: "1 1 16rem", minWidth: "12rem", fontWeight: running ? 600 : 400, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>⣿ {group.title}</span>
                  <Badge tone={running ? "running" : group.column === "completed" ? "completed" : "neutral"}>{running ? `${group.running} running` : group.column}</Badge>
                  <span style={{ color: "var(--text-muted)", fontSize: "var(--font-size-sm)" }}>{group.done}/{group.total} · {pct}%</span>
                  <span style={{ marginLeft: "auto", display: "inline-flex", gap: "var(--space-2)" }}>
                    {group.total > 0 ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-expanded={openSubtasks === group.runId}
                        aria-label={`${openSubtasks === group.runId ? "Hide" : "Show"} sub-tasks for ${group.title}`}
                        onClick={(event) => {
                          // Inside a <Link>: without this the disclosure would navigate away.
                          event.preventDefault();
                          event.stopPropagation();
                          setOpenSubtasks((current) => (current === group.runId ? null : group.runId));
                        }}
                      >
                        {openSubtasks === group.runId ? "▾" : "▸"} {group.total} sub-task{group.total === 1 ? "" : "s"}
                      </Button>
                    ) : null}
                    {running ? (
                      <Button variant="secondary" size="sm" disabled={busyId === group.runId} aria-label={`Pause swarm ${group.title}`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); void (async () => { setBusyId(group.runId); setNotice(null); try { const result = await cancelSwarmRun(group.runId); if (result === null) { setNotice("Cancel requested — result unknown; verify the run's sessions."); } else if (result.kill_complete === false) { const ids = unstoppedCancelIds(result); setNotice(`Partial cancel — not verifiably stopped: ${ids.length ? ids.join(", ") : (result.warning ?? "unknown")}. Press Pause again to re-check.`); } else { setNotice("Run paused."); } refresh(); } catch (reason) { setNotice(reason instanceof Error ? `Pause failed: ${reason.message}` : "Pause failed."); } finally { setBusyId(null); } })(); }}>
                        {busyId === group.runId ? "…" : "Pause"}
                      </Button>
                    ) : null}
                  </span>
                </>
              );
              return (
                <li key={group.runId} style={{ borderBottom: "1px solid var(--border)", padding: "var(--space-2) 0" }}>
                  {rootId ? (
                    <Link href={`/activity/${encodeURIComponent(rootId)}?kind=board`} style={{ color: "inherit", textDecoration: "none", display: "flex", alignItems: "center", gap: "var(--space-3)", flexWrap: "wrap" }} aria-label={`Open swarm ${group.title}`}>
                      {row}
                    </Link>
                  ) : (
                    <span style={{ display: "flex", alignItems: "center", gap: "var(--space-3)", flexWrap: "wrap" }}>{row}</span>
                  )}
                  {openSubtasks === group.runId ? <SubtaskList group={group} /> : null}
                </li>
              );
            })}
            {listed.map((task) => {
              const who = agentLabel(task, agents);
              const work = task.work ?? null;
              const progress = work && Number.isFinite(work.steps_total) ? work : task.run_progress;
              const pct = progress && progress.steps_total > 0 ? Math.round((progress.steps_done / progress.steps_total) * 100) : null;
              const category = task.category_id ? categoryMap[task.category_id] : null;
              const running = ["claimed", "in_progress"].includes(task.status);
              return (
                <li key={task.id} style={{ borderBottom: "1px solid var(--border)", padding: "var(--space-2) 0" }}>
                  <Link href={`/activity/${encodeURIComponent(task.id)}?kind=board`} style={{ color: "inherit", textDecoration: "none", display: "flex", alignItems: "center", gap: "var(--space-3)", flexWrap: "wrap" }} aria-label={`Open details for ${task.title}`}>
                    <StatusDot state={running ? "running" : task.status === "done" ? "completed" : task.status === "blocked" ? "failed" : "queued"} />
                    <span style={{ flex: "1 1 16rem", minWidth: "12rem", fontWeight: running ? 600 : 400, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{task.title}</span>
                    <Badge tone={runStateTone(work?.state ?? task.status)}>{(work?.state ?? task.status).replace(/_/g, " ")}</Badge>
                    {category ? <Pill tone="neutral" style={category.color ? { borderColor: category.color, color: category.color } : undefined}>{category.name}</Pill> : null}
                    {who ? <span style={{ color: "var(--text-muted)", fontSize: "var(--font-size-sm)" }}>{who}</span> : null}
                    {pct !== null ? <span style={{ color: "var(--text-muted)", fontSize: "var(--font-size-sm)" }}>{pct}%</span> : null}
                    {work?.current_step ? <span style={{ color: "var(--text-muted)", fontSize: "var(--font-size-sm)", flex: "1 1 100%", paddingLeft: "calc(var(--space-3) + 10px)" }}>Now: {work.current_step}</span> : null}
                    <span style={{ marginLeft: "auto", display: "inline-flex", gap: "var(--space-2)" }}>
                      {canPauseTask(task) ? (
                        <Button variant="secondary" size="sm" disabled={busyId === task.id} aria-label={`Pause ${task.title}`} onClick={(event) => void act(task.id, "pause", event)}>
                          {busyId === task.id ? "…" : "Pause"}
                        </Button>
                      ) : null}
                      {canResumeTask(task) ? (
                        <Button variant="secondary" size="sm" disabled={busyId === task.id} aria-label={`Resume ${task.title}`} onClick={(event) => void act(task.id, "resume", event)}>
                          {busyId === task.id ? "…" : "Resume"}
                        </Button>
                      ) : null}
                      {!task.archived ? (
                        <Button variant="ghost" size="sm" disabled={busyId === task.id} aria-label={`Archive ${task.title}`} onClick={(event) => void act(task.id, "archive", event)}>
                          {busyId === task.id ? "…" : "Archive"}
                        </Button>
                      ) : null}
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
          {listedTotal > LIST_LIMIT ? (
            <div style={{ paddingTop: "var(--space-2)", display: "flex", gap: "var(--space-3)", alignItems: "center" }}>
              <Button variant="ghost" size="sm" onClick={() => setShowAll((current) => !current)}>
                {showAll ? "Show fewer" : `Show all ${listedTotal}`}
              </Button>
              <Link href="/board" style={{ fontSize: "var(--font-size-sm)" }}>Open full board →</Link>
            </div>
          ) : (
            <div style={{ paddingTop: "var(--space-2)" }}>
              <Link href="/board" style={{ fontSize: "var(--font-size-sm)" }}>Open full board →</Link>
            </div>
          )}
        </Card>
      ) : null}

      {!error && expanded && tasks.length > 0 ? (
        <BoardKanban
          tasks={tasks}
          onRunMutated={refresh}
          agents={agents}
          categoryMap={categoryMap}
          phaseOverlay={phaseOverlay}
          onPause={(taskId, event) => void act(taskId, "pause", event)}
          onResume={(taskId, event) => void act(taskId, "resume", event)}
          onArchive={(taskId, event) => void act(taskId, "archive", event)}
          archivingId={busyId}
          pausingId={busyId}
        />
      ) : null}
    </section>
  );
}
