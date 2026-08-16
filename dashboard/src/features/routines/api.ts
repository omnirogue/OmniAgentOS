/** Fetch client for the real Routines API surface — see
 * omniagentos/api/routes/routines.py. LITERAL paths used below:
 *   GET    /api/routines
 *   POST   /api/routines
 *   GET    /api/routines/{id}
 *   GET    /api/routines/runs        (cross-routine aggregate, section B)
 *   POST   /api/routines/{id}/enable
 *   POST   /api/routines/{id}/disable
 *   GET    /api/routines/{id}/runs
 * covered end-to-end (including the validation-rejection paths) by
 * tests/routines/test_api.py against the real FastAPI app. */
import { apiUrl } from "../../lib/apiRoute";
import { API_BASE } from "../../lib/contracts";
import { fetchWithTimeout } from "../../lib/fetchTimeout";
import type { SystemJobsSnapshot } from "./systemJobs";
import type {
  CreateRoutineInput,
  LoopTemplate,
  RecentRunItem,
  Routine,
  RoutineRun,
} from "./types";

export class RoutinesApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail: unknown = null,
  ) {
    super(message);
    this.name = "RoutinesApiError";
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  // fix7: create/update/enable/disable/delete go same-origin (token-attached
  // proxy); reads stay direct to FastAPI.
  const res = await fetchWithTimeout(apiUrl(API_BASE, path, init?.method), {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    let message = res.statusText;
    let detail: unknown = null;
    try {
      const body = (await res.json()) as { error?: { message?: string; detail?: unknown } };
      message = body?.error?.message ?? message;
      detail = body?.error?.detail ?? null;
    } catch {
      // Non-JSON error body — fall back to statusText.
    }
    throw new RoutinesApiError(message, res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** Fixture fallback: when ``NEXT_PUBLIC_ROUTINES_FIXTURES=1``, the aggregate
 * endpoint is answered from a local fixture instead of hitting the API, so the
 * Loops page renders before the backend is wired (per swarm coordination rule C.5). */
const ROUTINES_FIXTURES =
  typeof process !== "undefined" &&
  process.env.NEXT_PUBLIC_ROUTINES_FIXTURES === "1";

const FIXTURE_RUNS: RecentRunItem[] = [
  {
    routine_id: "rtn_fixture_1",
    routine_name: "nightly-lint-fix",
    run_id: "run_001",
    gate_passed: true,
    accepted: true,
    cost_usd: 0.42,
    finished_at: new Date(Date.now() - 1000 * 60 * 8).toISOString(),
  },
  {
    routine_id: "rtn_fixture_2",
    routine_name: "weekly-dep-bump",
    run_id: "run_002",
    gate_passed: true,
    accepted: false,
    cost_usd: 1.18,
    finished_at: new Date(Date.now() - 1000 * 60 * 45).toISOString(),
  },
  {
    routine_id: "rtn_fixture_1",
    routine_name: "nightly-lint-fix",
    run_id: "run_003",
    gate_passed: false,
    accepted: false,
    cost_usd: 0.05,
    finished_at: new Date(Date.now() - 1000 * 60 * 60 * 3).toISOString(),
  },
];

async function readOrgdimsLoops(): Promise<LoopTemplate[]> {
  try {
    const body = await req<{ loops: LoopTemplate[] }>("/api/orgdims/loops");
    return body.loops;
  } catch {
    return [];
  }
}

/** Fixture fallback for the System loops section: when
 * ``NEXT_PUBLIC_SYSTEM_JOBS_FIXTURES=1`` the snapshot is served locally so the
 * panel renders before ``omniagentos/api/routes/system_jobs.py`` is registered
 * in the API (swarm coordination rule C.5 — labeled fixture fallback). */
const SYSTEM_JOBS_FIXTURES =
  typeof process !== "undefined" &&
  process.env.NEXT_PUBLIC_SYSTEM_JOBS_FIXTURES === "1";

const FIXTURE_SYSTEM_JOBS: SystemJobsSnapshot = {
  generated_at: new Date().toISOString(),
  counts: { total: 2, loaded: 1, failing: 1, stale: 0 },
  jobs: [
    {
      key: "routines-tick",
      name: "Routines engine tick",
      executor: "launchd",
      category: "Routines engine",
      label: "com.omniagentos.routines",
      purpose: "Fires every due DB routine, then settles finished runs against their gates.",
      source: "scripts/scheduler/install-routines.sh",
      module: "omniagentos.scheduler.routines_tick",
      schedule: { kind: "interval", seconds: 300, description: "every 5 minutes" },
      env_overrides: [],
      loaded: true,
      plist_present: true,
      last_exit_status: 0,
      last_run_at: new Date(Date.now() - 1000 * 60 * 4).toISOString(),
      next_fire_at: new Date(Date.now() + 1000 * 60).toISOString(),
      health: "healthy",
      health_reason: "Fired within its expected cadence; last exit 0.",
      managed_candidate: false,
      candidate_reason: "This IS the routines engine.",
    },
    {
      key: "reflection-nightly",
      name: "Nightly reflection",
      executor: "launchd",
      category: "Reflection",
      label: "com.omniagentos.reflection-nightly",
      purpose: "Nightly reflection pass (observe-only).",
      source: "scripts/reflection/install-reflection.sh",
      module: "scripts/reflection/reflect-nightly.sh",
      schedule: { kind: "calendar", seconds: null, description: "daily 02:30 local" },
      env_overrides: ["OMNIAGENTOS_REFLECTION_NIGHTLY_HOUR"],
      loaded: true,
      plist_present: true,
      last_exit_status: 126,
      last_run_at: new Date(Date.now() - 1000 * 60 * 60 * 6).toISOString(),
      next_fire_at: new Date(Date.now() + 1000 * 60 * 60 * 18).toISOString(),
      health: "failing",
      health_reason: "launchd reports last exit status 126.",
      managed_candidate: true,
      candidate_reason: "Script invoked via /bin/sh (exit-126-safe); exit_code gate.",
    },
  ],
};

async function readSystemJobs(): Promise<SystemJobsSnapshot> {
  if (SYSTEM_JOBS_FIXTURES) return FIXTURE_SYSTEM_JOBS;
  return req<SystemJobsSnapshot>("/api/system-jobs");
}

export const routinesApi = {
  list: (status?: string) =>
    req<Routine[]>(`/api/routines${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  get: (id: string) => req<Routine>(`/api/routines/${encodeURIComponent(id)}`),
  create: (input: CreateRoutineInput) =>
    req<Routine>("/api/routines", { method: "POST", body: JSON.stringify(input) }),
  update: (id: string, fields: Partial<CreateRoutineInput>) =>
    req<Routine>(`/api/routines/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(fields),
    }),
  remove: (id: string) =>
    req<void>(`/api/routines/${encodeURIComponent(id)}`, { method: "DELETE" }),
  enable: (id: string) =>
    req<Routine>(`/api/routines/${encodeURIComponent(id)}/enable`, { method: "POST" }),
  disable: (id: string) =>
    req<Routine>(`/api/routines/${encodeURIComponent(id)}/disable`, { method: "POST" }),
  runs: (id: string) => req<RoutineRun[]>(`/api/routines/${encodeURIComponent(id)}/runs`),

  /** Cross-routine recent runs aggregate (section B contract). Falls back to
   * a local fixture when ``NEXT_PUBLIC_ROUTINES_FIXTURES=1`` so the Loops
   * page renders before the backend lands. */
  recentRuns: async (limit = 50): Promise<RecentRunItem[]> => {
    if (ROUTINES_FIXTURES) return FIXTURE_RUNS.slice(0, limit);
    const body = await req<{ runs: RecentRunItem[] }>(
      `/api/routines/runs?limit=${encodeURIComponent(String(limit))}`,
    );
    return body.runs;
  },

  /** Loop templates recommended by the orgdims service — surfaced on the
   * Loops page as a "Recommended" section with an "Accept" action. */
  recommendedLoops: readOrgdimsLoops,

  /** Read-only snapshot of every scheduled job on the system (launchd agents,
   * CSI pipeline routines, documented remote cron) — the "System loops"
   * section. Contract: omniagentos/api/routes/system_jobs.py. */
  systemJobs: readSystemJobs,
};

/** Every validation error string returned by the backend (e.g. "gate_type must be
 * one of [...]") — used to render the full list, not just the first message. */
export function validationErrors(error: unknown): string[] {
  if (error instanceof RoutinesApiError && Array.isArray(error.detail)) {
    return error.detail.filter((item): item is string => typeof item === "string");
  }
  return [];
}
