/** Wire types for the W2 projects surface — mirrors omniagentos/api/routes/projects.py. */

import type { ActionClass, RunState, StepStatus, TaskState } from "../../lib/contracts";

export type ProjectGrant = {
  project_id: string;
  action_class: string;
  requires_approval: boolean;
  always_human: boolean;
};

export type ResolvedActionPolicy = {
  requires_approval: boolean;
  always_human: boolean;
};

export type ResolvedPolicy = Record<string, ResolvedActionPolicy>;

export type Project = {
  id: string;
  name: string;
  root_dirs: string[];
  vault_subfolder: string;
  budget_usd: number | null;
  allowed_tools: string[];
  allowed_dirs: string[];
  created_at: string;
  grants: ProjectGrant[];
  resolved_policy?: ResolvedPolicy;
  org_company_id?: string | null;
};

export type GrantInput = {
  action_class: string;
  requires_approval?: boolean;
  always_human?: boolean;
};

export type CreateProjectInput = {
  name: string;
  root_dirs?: string[];
  vault_subfolder?: string;
  budget_usd?: number | null;
  allowed_tools?: string[];
  allowed_dirs?: string[];
  grants?: GrantInput[];
};

/** GET /api/projects/{id}/activity — a project's live progress. Feature-local
 * (not lib/contracts.ts, which is frozen) mirroring
 * omniagentos.projects.activity.build_project_activity's response shape. */

export type ProjectActivityStep = {
  id: number;
  run_id: string;
  seq: number;
  name: string;
  action_class: ActionClass;
  status: StepStatus;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
};

export type ProjectActivityLogLine = {
  id: number;
  ts: string;
  line: string;
};

/** One entry in the project-wide merged feed — the same line shape as a
 * per-run log_tail entry, plus which task/run it belongs to. */
export type ProjectActivityLogEntry = ProjectActivityLogLine & {
  task_id: string | null;
  run_id: string | null;
};

export type ProjectActivityRun = {
  id: string;
  task_id: string;
  state: RunState;
  harness: string;
  arm: string | null;
  model: string | null;
  agent: string | null;
  worker_id: string | null;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  cost_usd: number | null;
  error: string | null;
  steps: ProjectActivityStep[];
  steps_done: number;
  steps_total: number;
  /** The step currently `started` (executing), if any. */
  current_step: ProjectActivityStep | null;
  /** This run's own events, newest-first, bounded. */
  log_tail: ProjectActivityLogLine[];
};

export type ProjectActivityTask = {
  id: string;
  title: string;
  state: TaskState;
  risk: string;
  discipline_id: string | null;
  created_at: string;
  updated_at: string;
  /** Newest-first. */
  runs: ProjectActivityRun[];
};

export type ProjectActivitySummary = {
  tasks: number;
  runs: number;
  running: number;
  awaiting_approval: number;
  completed: number;
  failed: number;
};

export type ProjectActivity = {
  project_id: string;
  project_name?: string;
  generated_at: string;
  /** Newest-first (by task updated_at); each task's runs are newest-first too. */
  tasks: ProjectActivityTask[];
  /** The whole project's run/step/task/approval events merged, newest-first, bounded. */
  activity_log: ProjectActivityLogEntry[];
  summary: ProjectActivitySummary;
};
