/**
 * W3 project-hierarchy client — the tree, per-node conversations and activity.
 *
 * Wire types mirror the FROZEN W3-hierarchy contract:
 *   GET  /api/projects/tree                    -> nested tree, top-level first
 *   GET  /api/projects/{id}/conversation       -> ordered messages
 *   GET  /api/tasks/{id}/conversation          -> ordered messages
 *   POST /api/projects/{id}/message  {role,content,model?}
 *   POST /api/tasks/{id}/message     {role,content,model?}
 *
 * Activity is a best-effort read of /api/projects/{id}/activity (projlogs); when
 * that endpoint is absent it is derived from the project's runs so the tab still
 * renders. Everything routes through the shared fetch-timeout wrapper so a wedged
 * backend surfaces as a bounded error, never an infinite spinner.
 */

import { apiUrl } from "../../lib/apiRoute";
import { API_BASE, type RunSummary, type TaskState } from "../../lib/contracts";
import { fetchWithTimeout } from "../../lib/fetchTimeout";
import { ProjectApiError } from "./api";
import type { Project } from "./types";

export type ConversationScope = "project" | "task";
export type ConversationRole = "user" | "agent" | "system";

/**
 * A single task leaf as returned inside a tree node.
 *
 * Dependency fields (`depends_on`, `blocked_by`) are OPTIONAL forward-compat
 * slots. The frozen W3/W4 tree payload does NOT currently emit them — the UI
 * renders a clean affordance only when they are present and non-empty. Do not
 * fabricate edges client-side.
 */
export type TreeTask = {
  id: string;
  title: string;
  state: TaskState;
  /** Task ids this task waits on (not yet in live API payload). */
  depends_on?: string[];
  /** Task ids blocking this task (not yet in live API payload). */
  blocked_by?: string[];
};

/** One node of GET /api/projects/tree. `project` carries at least id + name;
 * the rest of the Project shape is optional so a lean backend payload still
 * satisfies the type.
 *
 * Project-level dependency fields are likewise optional and unused by the
 * current API — documented here so a future backend revision can ship without
 * a frontend type break. */
export type ProjectTreeNode = {
  project: Pick<Project, "id" | "name"> & Partial<Project> & { parent_project_id?: string | null };
  status: string;
  sub_projects: ProjectTreeNode[];
  tasks: TreeTask[];
  /** Project ids this node depends on (not yet in live API payload). */
  depends_on?: string[];
  /** Project ids blocking this node (not yet in live API payload). */
  blocked_by?: string[];
};

/** A row of conversations(scope_type, scope_id, seq, role, content, model, …). */
export type ConversationMessage = {
  id: string;
  scope_type: ConversationScope;
  scope_id: string;
  seq: number;
  role: ConversationRole;
  content: string;
  model: string | null;
  created_at: string;
  meta_json?: string;
};

export type PostMessageInput = {
  role: ConversationRole;
  content: string;
  model?: string | null;
};

/** Normalised activity row — either a projlogs entry or a derived run summary. */
export type ActivityEntry = {
  id: string;
  ts: string;
  kind: string;
  title: string;
  detail?: string | null;
  state?: string | null;
};

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  // fix7: project/task message posts go same-origin (token proxy); reads direct.
  const res = await fetchWithTimeout(apiUrl(API_BASE, path, init?.method), {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.error?.message ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new ProjectApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

const enc = encodeURIComponent;

export function fetchProjectTree(): Promise<ProjectTreeNode[]> {
  return req<ProjectTreeNode[]>("/api/projects/tree");
}

export function fetchConversation(
  scope: ConversationScope,
  id: string,
): Promise<ConversationMessage[]> {
  const base = scope === "task" ? "tasks" : "projects";
  return req<ConversationMessage[]>(`/api/${base}/${enc(id)}/conversation`);
}

export function postConversationMessage(
  scope: ConversationScope,
  id: string,
  input: PostMessageInput,
): Promise<ConversationMessage> {
  const base = scope === "task" ? "tasks" : "projects";
  return req<ConversationMessage>(`/api/${base}/${enc(id)}/message`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

type RawActivity = { entries?: unknown; activity?: unknown } | unknown[];

function normaliseActivity(raw: RawActivity): ActivityEntry[] {
  const list = Array.isArray(raw)
    ? raw
    : Array.isArray((raw as { entries?: unknown })?.entries)
      ? (raw as { entries: unknown[] }).entries
      : Array.isArray((raw as { activity?: unknown })?.activity)
        ? (raw as { activity: unknown[] }).activity
        : [];
  return list.map((item, idx) => {
    const row = (item ?? {}) as Record<string, unknown>;
    const ts =
      (row.ts as string) ??
      (row.created_at as string) ??
      (row.at as string) ??
      new Date().toISOString();
    const kind =
      (row.type as string) ?? (row.kind as string) ?? (row.action as string) ?? "event";
    const title =
      (row.action as string) ??
      (row.summary as string) ??
      (row.title as string) ??
      (row.message as string) ??
      kind;
    return {
      id: String(row.id ?? `${ts}-${idx}`),
      ts,
      kind,
      title,
      detail:
        (row.detail as string) ??
        (row.target_id as string) ??
        (row.actor as string) ??
        null,
      state: (row.state as string) ?? (row.status as string) ?? null,
    };
  });
}

function runsToActivity(runs: RunSummary[]): ActivityEntry[] {
  return runs
    .map((run) => ({
      id: run.id,
      ts: run.finished_at ?? run.started_at ?? run.queued_at,
      kind: "run",
      title: `${run.harness} run ${run.state.replace(/_/g, " ")}`,
      detail: run.agent ?? run.model ?? run.task_id,
      state: run.state,
    }))
    .sort((a, b) => (a.ts < b.ts ? 1 : -1));
}

/**
 * Best-effort activity for a project. Tries the projlogs endpoint; if it is
 * missing (404) or otherwise unavailable, derives a feed from the project's
 * runs so the tab is never empty when there is real work to show. A timeout /
 * network failure is re-thrown so the caller can fall back to fixtures.
 */
export async function fetchProjectActivity(projectId: string): Promise<ActivityEntry[]> {
  try {
    const raw = await req<RawActivity>(`/api/projects/${enc(projectId)}/activity`);
    return normaliseActivity(raw);
  } catch (reason) {
    if (reason instanceof ProjectApiError && reason.status === 404) {
      const runs = await req<RunSummary[]>(
        `/api/runs?project_id=${enc(projectId)}`,
      );
      return runsToActivity(runs);
    }
    throw reason;
  }
}

/* ------------------------------------------------------------------ tones -- */

export type StatePresentation = { tone: string; label: string };

/** Map a task/project state string to a Badge/StatusDot tone. Unknown states
 * fall back to a neutral pill rather than crashing on an un-themed class. */
export function stateTone(state: string): string {
  switch (state) {
    case "completed":
      return "completed";
    case "running":
      return "running";
    case "failed":
      return "failed";
    case "cancelled":
      return "cancelled";
    case "paused":
      return "paused";
    case "validating":
      return "validating";
    case "queued":
    case "ready":
      return "queued";
    case "awaiting_approval":
    case "reviewing":
    case "revision_requested":
      return "awaiting_approval";
    case "retrying":
      return "running";
    case "draft":
    default:
      return "neutral";
  }
}

/** Progress for a node: fraction of its (recursive) tasks that are completed. */
export function nodeProgress(node: ProjectTreeNode): { done: number; total: number } {
  let done = 0;
  let total = 0;
  const walk = (n: ProjectTreeNode) => {
    for (const t of n.tasks) {
      total += 1;
      if (t.state === "completed") done += 1;
    }
    n.sub_projects.forEach(walk);
  };
  walk(node);
  return { done, total };
}
