import { apiUrl } from "@/lib/apiRoute";
import { API_BASE, type HarnessType } from "@/lib/contracts";
import { fetchWithTimeout } from "@/lib/fetchTimeout";

/** Server-side title cap (mirrors CreateTaskRequest) — dispatch is rejected client-side above this. */
export const MAX_TASK_TITLE_LENGTH = 2000;

const MAX_ERROR_DETAIL_LENGTH = 200;

function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

async function extractErrorDetail(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { error?: { message?: string }; message?: string } | null;
    const message = body?.error?.message ?? body?.message;
    if (typeof message === "string" && message.trim()) return truncate(message, MAX_ERROR_DETAIL_LENGTH);
  } catch {
    // Malformed/non-JSON body (e.g. an HTML 500 page) — fall through to statusText.
  }
  return res.statusText || `Request failed (${res.status})`;
}

/**
 * Intake clarify/dispatch invoke an LLM (Fable/Claude planner) server-side, which
 * routinely takes 15–60s — far longer than the 10s data-endpoint default that
 * exists to kill infinite spinners. These calls get their own generous budget so
 * the front door doesn't abort a healthy request mid-think.
 */
const INTAKE_LLM_TIMEOUT_MS = 90_000;

/**
 * The plan job kickoff/poll/confirm calls never hold a request open across an LLM
 * call (POST /intake/plan only registers a background job and returns; GET polls
 * its status; confirm only writes rows + dispatches). They still get their own
 * (shorter, but above the 10s data-endpoint default) budget rather than sharing
 * INTAKE_LLM_TIMEOUT_MS, since none of them are actually LLM-bound — a plan with
 * many sub-projects/tasks just needs a bit more room than a single GET.
 */
const PLAN_JOB_TIMEOUT_MS = 15_000;
const PLAN_CONFIRM_TIMEOUT_MS = 30_000;

async function req<T>(path: string, init?: RequestInit, timeoutMs?: number): Promise<T> {
  // fix7: intake mutations (clarify/dispatch/plan/confirm) are now token-gated, so
  // they go SAME-ORIGIN to the Next.js proxy (which attaches the token); reads
  // (disciplines, plan status) stay direct to FastAPI. proxyReq below is the
  // pre-existing same-origin path for the already-gated /api/tasks routes.
  const res = await fetchWithTimeout(apiUrl(API_BASE, path, init?.method), {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  }, timeoutMs);
  if (!res.ok) {
    const detail = await extractErrorDetail(res);
    throw new Error(`${res.status}: ${detail}`);
  }
  return (await res.json()) as T;
}

/**
 * fix6: POST /api/tasks and /api/tasks/{id}/runs are now token-gated on the
 * FastAPI side (an agent that loopback-POSTs them is rejected). These dispatch
 * mutations therefore go SAME-ORIGIN to the Next.js route handlers
 * (dashboard/src/app/api/**) which read the local session token server-side and
 * inject it — never through ${API_BASE}, which would hit FastAPI directly
 * without the token.
 */
async function proxyReq<T>(path: string, init?: RequestInit, timeoutMs?: number): Promise<T> {
  const res = await fetchWithTimeout(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  }, timeoutMs);
  if (!res.ok) {
    const detail = await extractErrorDetail(res);
    throw new Error(`${res.status}: ${detail}`);
  }
  return (await res.json()) as T;
}

export interface Discipline {
  id: string;
  name: string;
  status: string;
}

export interface DispatchedTask {
  id: string;
  title: string;
  state: string;
  discipline_id: string | null;
}

export interface DispatchedRun {
  id: string;
  task_id: string;
  harness: HarnessType;
  state: string;
}

/** Human labels for HarnessType — the "lane" a dispatched task runs on. */
export const LANE_OPTIONS: Array<{ value: HarnessType; label: string }> = [
  { value: "cli-claude", label: "Claude CLI" },
  { value: "cli-codex", label: "Codex CLI" },
  { value: "cli-grok", label: "Grok CLI" },
  { value: "mini-swe", label: "Mini-SWE" },
  { value: "openhands", label: "OpenHands" },
  { value: "fusion", label: "Fusion" },
  { value: "improve", label: "Improve" },
  { value: "mock", label: "Mock (dry run)" },
];

/** One prior clarifying question the agent asked and the operator's answer. */
export interface ClarifyTurn {
  q: string;
  a: string;
}

/** The precise task spec the clarifying agent produces once it has enough. */
export interface RefinedSpec {
  title: string;
  description: string;
  acceptance_criteria: string[];
  suggested_discipline: string | null;
  suggested_priority: string;
  required_expertise: string[];
}

/** One turn of the clarify loop: either questions, or a finished spec. */
export interface ClarifyResult {
  mode: "questions" | "spec";
  questions: string[];
  spec: RefinedSpec | null;
  round: number;
  forced: boolean;
}

/**
 * Execution posture for a dispatched task.
 * - "readonly" (default): the run's agent only generates text — safe, auto-approved.
 * - "tools": the run can read/write files and run shell in a scoped working dir,
 *   but the runner parks it for HUMAN APPROVAL before anything runs.
 */
export type ExecuteMode = "readonly" | "tools";

/**
 * The Mode dial's speed knob (D10), mirrored from DispatchRequest.speed. Maps
 * server-side onto the orchestrator priority (fast→'fast', auto→'balanced',
 * ultra→'quality') and the planner chain (fast plans on gemini-3.6-flash).
 * A non-default speed wins over any legacy priority; "auto" is the safe default.
 */
export type Speed = "fast" | "auto" | "ultra";

/** Result of POST /api/intake/dispatch — a live board card plus its executing run. */
export interface DispatchedIntake {
  board_task: { id: string; title: string; status: string; run_id: string | null };
  task_id: string;
  run_id: string;
  execute: ExecuteMode;
  working_dir: string | null;
}

/* ------------------------------------------------------- Fable plan flow -- */
/** Wire types mirror omniagentos/intake/planner.py's ProjectPlan/RouteDecision. */

export interface PlannedTask {
  title: string;
  description: string;
  acceptance_criteria: string[];
  suggested_discipline: string | null;
  suggested_priority: string;
}

export interface PlannedSubProject {
  name: string;
  description: string;
  tasks: PlannedTask[];
}

export type PlanComplexity = "simple" | "standard" | "complex";

export interface ProjectPlan {
  project_name: string;
  description: string;
  complexity: PlanComplexity;
  effort: "low" | "medium" | "high" | "max";
  tasks: PlannedTask[];
  sub_projects: PlannedSubProject[];
}

export interface RouteDecision {
  decision: "new" | "existing";
  parent_project_id: string | null;
  project_name: string;
  reason: string;
}

export type PlanJobStatus = "running" | "ready" | "error" | "confirmed";

/** GET /api/intake/plan/{job_id} — the plan job's current state. */
export interface PlanJobState {
  job_id: string;
  status: PlanJobStatus;
  plan: ProjectPlan | null;
  route: RouteDecision | null;
  /** Resolved name of route.parent_project_id when decision === "existing"; null
   * if it could not be resolved (e.g. the project was deleted mid-flight). */
  route_target_name: string | null;
  error: string | null;
}

/** One task dispatch_spec() produced while provisioning a confirmed plan. */
export interface PlanDispatchedTask {
  board_task: { id: string; title: string; status: string; run_id: string | null };
  task_id: string;
  run_id: string;
  execute: ExecuteMode;
  working_dir: string | null;
  project_id?: string | null;
}

/** Result of POST /api/intake/plan/{job_id}/confirm — the materialised plan. */
export interface PlanProvisionResult {
  root_project_id: string;
  route: RouteDecision;
  sub_project_ids: Record<string, string>;
  task_count: number;
  dispatched: PlanDispatchedTask[];
}

/**
 * The clarifying-intake surface: a cheap agent refines a rough request into a
 * precise spec (POST /api/intake/clarify), then the confirmed spec becomes a
 * live board card + control-plane run (POST /api/intake/dispatch).
 */
export const intakeApi = {
  clarify: (draft: string, history: ClarifyTurn[]) =>
    req<ClarifyResult>("/api/intake/clarify", {
      method: "POST",
      body: JSON.stringify({ draft, history }),
    }, INTAKE_LLM_TIMEOUT_MS),
  // `projectId` scopes the dispatched task to a real project (DispatchRequest's
  // own project_id field) — previously this slot fed a chosen id into
  // suggested_discipline instead, a reported bug (W4-frontdoor): the "Project"
  // selector never actually set a project, and stomped the clarify agent's own
  // discipline suggestion whenever something was picked.
  dispatch: (
    spec: RefinedSpec,
    harness?: HarnessType,
    projectId?: string,
    execute: ExecuteMode = "readonly",
    speed: Speed = "auto",
  ) =>
    req<DispatchedIntake>("/api/intake/dispatch", {
      method: "POST",
      body: JSON.stringify({
        title: spec.title,
        description: spec.description,
        acceptance_criteria: spec.acceptance_criteria,
        suggested_discipline: spec.suggested_discipline || undefined,
        suggested_priority: spec.suggested_priority,
        required_expertise: spec.required_expertise,
        harness: harness || undefined,
        project_id: projectId || undefined,
        execute,
        speed,
      }),
    }, INTAKE_LLM_TIMEOUT_MS),
};

/**
 * The Fable plan flow — the front door's primary path (W4-frontdoor): a goal
 * becomes a job (POST), the dashboard polls it (GET) until Fable's PLAN
 * (sub-projects + tasks) and ROUTE decision (new project vs. nest under an
 * existing one) are ready, then the operator confirms (POST .../confirm) to
 * actually create the hierarchy and dispatch every task.
 */
export const planApi = {
  // D10: the dial's speed is sent AT KICKOFF — PlanRequest persists it on the
  // plan job (with execute) so both the background planning pass and the
  // approval-time dispatch honor the dial as chosen when planning started.
  start: (goal: string, speed?: Speed) =>
    req<{ job_id: string; status: PlanJobStatus }>("/api/intake/plan", {
      method: "POST",
      body: JSON.stringify({ goal, speed }),
    }, PLAN_JOB_TIMEOUT_MS),
  status: (jobId: string) =>
    req<PlanJobState>(`/api/intake/plan/${encodeURIComponent(jobId)}`, undefined, PLAN_JOB_TIMEOUT_MS),
  confirm: (
    jobId: string,
    input: { projectOverride: string; harness?: HarnessType; execute?: ExecuteMode; speed?: Speed },
  ) =>
    req<PlanProvisionResult>(`/api/intake/plan/${encodeURIComponent(jobId)}/confirm`, {
      method: "POST",
      body: JSON.stringify({
        project_override: input.projectOverride,
        harness: input.harness || undefined,
        execute: input.execute,
        // D10: PlanConfirmRequest.speed OVERRIDES the speed persisted on the
        // plan job (the UI sends the same value on both calls); the server
        // maps speed → priority/pins at the API edge before dispatching.
        speed: input.speed,
      }),
    }, PLAN_CONFIRM_TIMEOUT_MS),
};

export const voiceDispatchApi = {
  disciplines: () => req<Discipline[]>("/api/disciplines"),
  createTask: (input: { title: string; disciplineId?: string }) =>
    proxyReq<DispatchedTask>("/api/tasks", {
      method: "POST",
      body: JSON.stringify({
        title: input.title,
        discipline_id: input.disciplineId || undefined,
        input: { text: input.title, source: "voice-intake" },
      }),
    }),
  createRun: (taskId: string, harness: HarnessType, prompt: string) =>
    proxyReq<DispatchedRun>(`/api/tasks/${encodeURIComponent(taskId)}/runs`, {
      method: "POST",
      body: JSON.stringify({ harness, prompt }),
    }),
};
