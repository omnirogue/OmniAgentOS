"use client";

/**
 * CompanyGoalsTree (P5) — company → goal → task → subtask, from
 * `GET /api/team/tree`. Mounted above the existing Steward goals list on
 * `/goals`. Task rows deep-link `?task=<id>` (the same drawer convention
 * `/board` uses).
 *
 * Deviation from the brief: goal rows carry no `owner` chip. `company_goals`
 * (migration 098) has no `owner_employee_id` column — only board_tasks
 * (migration 123) does — so `GET /api/team/tree`'s goal nodes never carry
 * one (see `CompanyGoalsStore.list_goals`'s `SELECT *`). Task rows DO carry
 * `owner_employee_id` and render it.
 */

import { useState } from "react";
import { Badge, EmptyState, ErrorState, Loading, StatusDot } from "@/design";
import type { StatusDotState } from "@/design";
import { useTeamTree } from "./hooks";
import { employeeName } from "./types";
import type { TeamTreeGoal, TeamTreeTask } from "./types";
import styles from "./team.module.css";

function statusDotState(status: string): StatusDotState {
  switch (status) {
    case "done":
      return "completed";
    case "blocked":
      return "danger";
    case "claimed":
    case "in_progress":
      return "running";
    case "awaiting_approval":
      return "awaiting_approval";
    case "cancelled":
      return "cancelled";
    default:
      return "queued";
  }
}

function TaskNode({ task, onOpen }: { task: TeamTreeTask; onOpen: (taskId: string) => void }) {
  return (
    <div>
      <button
        type="button"
        className={styles.treeTaskRow}
        onClick={() => onOpen(task.id)}
        aria-label={`Open ${task.title}`}
      >
        <StatusDot state={statusDotState(task.status)} label={task.status.replace(/_/g, " ")} />
        {task.ref ? <span className={styles.evidenceRef}>{task.ref}</span> : null}
        <span className={styles.treeTaskTitle}>{task.title}</span>
        {task.owner_employee_id ? (
          <span className={styles.personCount}>{employeeName(task.owner_employee_id) ?? task.owner_employee_id}</span>
        ) : null}
      </button>
      {task.subtasks.length > 0 ? (
        <div className={styles.treeSubtasks}>
          {task.subtasks.map((subtask) => (
            <TaskNode key={subtask.id} task={subtask} onOpen={onOpen} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function GoalNode({ goal, onOpen }: { goal: TeamTreeGoal; onOpen: (taskId: string) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={styles.treeGoal}>
      <button type="button" className={styles.treeGoalHead} aria-expanded={open} onClick={() => setOpen((current) => !current)}>
        <span aria-hidden="true">{open ? "⌄" : "›"}</span>
        <span className={styles.treeGoalTitle}>{goal.title}</span>
        <Badge tone="neutral">{goal.horizon.replace(/_/g, " ")}</Badge>
      </button>
      {open ? (
        <div className={styles.treeTasks}>
          {goal.tasks.length === 0 ? (
            <p className={styles.treeEmpty}>No tasks yet.</p>
          ) : (
            goal.tasks.map((task) => <TaskNode key={task.id} task={task} onOpen={onOpen} />)
          )}
        </div>
      ) : null}
    </div>
  );
}

export function CompanyGoalsTree({ onOpenTask }: { onOpenTask: (taskId: string) => void }) {
  const { tree, loading, error, hasLoaded, refresh } = useTeamTree();

  if (loading && !hasLoaded) {
    return <Loading variant="skeleton" label="Loading company goals" lines={4} />;
  }
  if (error && !hasLoaded) {
    return <ErrorState message={error} onRetry={() => void refresh()} />;
  }
  if (tree.companies.length === 0) {
    return <EmptyState title="No company goals yet" message="Company goals will appear here once they're created." />;
  }

  return (
    <div className={styles.tree}>
      {tree.companies.map((company) => (
        <div key={company.id} className={styles.treeCompany}>
          <h3 className={styles.treeCompanyName}>{company.name}</h3>
          {company.goals.length === 0 ? (
            <p className={styles.treeEmpty}>No goals yet.</p>
          ) : (
            company.goals.map((goal) => <GoalNode key={goal.id} goal={goal} onOpen={onOpenTask} />)
          )}
        </div>
      ))}
    </div>
  );
}
