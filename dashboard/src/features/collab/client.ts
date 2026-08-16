import { apiUrl } from "@/lib/apiRoute";
import { fetchWithTimeout } from "@/lib/fetchTimeout";
import { FIXTURE_AGENTS, FIXTURE_CHANNELS, FIXTURE_LIVE_TASKS, FIXTURE_MESSAGES, USE_FIXTURES } from "./fixtures";
import { API_BASE } from "@/lib/contracts";
import type { Agent, ArchiveTasksResult, BoardFilesResponse, BoardTaskSessions, Channel, ClaimResult, LedgerClaim, LedgerEvent, LiveBoardTask, Message, MessageKind, TaskCategory, TaskTurn, LonghaulTaskDetail } from "./types";

export class CollabApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "CollabApiError";
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  // fix7: agent/board/channel/message mutations go same-origin (token proxy);
  // reads stay direct to FastAPI.
  const isForm = init?.body instanceof FormData;
  const res = await fetchWithTimeout(apiUrl(API_BASE, path, init?.method), {
    ...init,
    headers: isForm ? { ...(init?.headers ?? {}) } : { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = "";
    try {
      const body = (await res.json()) as { error?: { message?: string } };
      detail = body?.error?.message ?? JSON.stringify(body);
    } catch {
      detail = res.statusText;
    }
    throw new CollabApiError(`${res.status}: ${detail}`, res.status);
  }
  return (await res.json()) as T;
}

function boardFilesPath(taskId: string, suffix = ""): string {
  return `/api/board/${encodeURIComponent(taskId)}/files${suffix}`;
}

/** What one conditional board read returned. */
export interface ConditionalBoardResult {
  /** null ⟺ the server answered 304 and the caller must keep what it has. */
  tasks: LiveBoardTask[] | null;
  /** The validator to send back next time; null when the server sent none. */
  etag: string | null;
  notModified: boolean;
}

/**
 * The board read, CONDITIONAL. `GET /api/board` ships a weak ETag over every
 * input its reconciler touches; sending the previous one back as
 * `If-None-Match` lets the server answer `304` with no body when nothing moved.
 * The board polls every 30s and refetches on every SSE frame, and the payload
 * is megabytes — so the overwhelmingly common answer ("still the same board")
 * should cost a header exchange, not a full re-serialize plus a re-parse plus
 * a whole-tree re-render.
 *
 * A 304 returns `tasks: null`; the caller keeps its current state. It is NOT an
 * empty board, and conflating the two would blank the kanban every 30 seconds.
 */
export async function liveBoardConditional(params?: {
  archived?: boolean;
  project_id?: string;
  etag?: string | null;
}): Promise<ConditionalBoardResult> {
  if (USE_FIXTURES) {
    return {
      tasks: FIXTURE_LIVE_TASKS
        .filter((task) => Boolean(task.archived) === Boolean(params?.archived))
        .filter((task) => !params?.project_id || task.project_id === params.project_id),
      etag: null,
      notModified: false,
    };
  }
  const qs = new URLSearchParams();
  if (params?.archived) qs.set("archived", "1");
  if (params?.project_id) qs.set("project_id", params.project_id);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const response = await fetchWithTimeout(apiUrl(API_BASE, `/api/board${suffix}`, "GET"), {
    headers: {
      "Content-Type": "application/json",
      ...(params?.etag ? { "If-None-Match": params.etag } : {}),
    },
    cache: "no-store",
  });
  if (response.status === 304) {
    // The server echoes the tag it matched; keep the old one if it did not.
    return { tasks: null, etag: response.headers.get("ETag") ?? params?.etag ?? null, notModified: true };
  }
  if (!response.ok) {
    let detail = "";
    try {
      const body = (await response.json()) as { error?: { message?: string } };
      detail = body?.error?.message ?? JSON.stringify(body);
    } catch {
      detail = response.statusText;
    }
    throw new CollabApiError(`${response.status}: ${detail}`, response.status);
  }
  return {
    tasks: (await response.json()) as LiveBoardTask[],
    etag: response.headers.get("ETag"),
    notModified: false,
  };
}

export function boardFileDownloadUrl(taskId: string, rel: string): string {
  const query = new URLSearchParams({ path: rel }).toString();
  return apiUrl(API_BASE, `${boardFilesPath(taskId, "/download")}?${query}`);
}

export function boardArchiveUrl(taskId: string): string {
  return apiUrl(API_BASE, boardFilesPath(taskId, "/archive"));
}

export function boardFilesShareUrl(taskId: string): string {
  const path = `/board?task=${encodeURIComponent(taskId)}&files=1`;
  return typeof window === "undefined" ? path : `${window.location.origin}${path}`;
}

export function fetchBoardFiles(taskId: string): Promise<BoardFilesResponse> {
  return req<BoardFilesResponse>(boardFilesPath(taskId));
}

export type RevealApp = "finder" | "vscode" | "cursor" | "terminal";

export interface RevealResult {
  revealed: boolean;
  app: string;
  path: string;
  workspace: string;
}

/**
 * Open a card's workspace (or a file in it) in a local macOS app — Reveal in
 * Finder / Open in VS Code · Cursor · Terminal. A localhost-only convenience:
 * the SERVER opens it because the browser cannot. The mutation goes SAME-ORIGIN
 * through the dedicated `app/api/board/[id]/files/reveal` proxy route (the
 * catch-all authorizes GET prefixes only), which injects the session token
 * server-side. `path` is workspace-relative (omit it for the workspace root);
 * `app` defaults server-side to `finder`.
 */
export function revealBoardWorkspace(
  taskId: string,
  options?: { path?: string; app?: RevealApp },
): Promise<RevealResult> {
  return req<RevealResult>(boardFilesPath(taskId, "/reveal"), {
    method: "POST",
    body: JSON.stringify({ path: options?.path, app: options?.app }),
  });
}

export function uploadBoardFiles(
  taskId: string,
  files: File[],
  instructions = "",
  onProgress?: (percent: number) => void,
): Promise<{ saved: Array<{ name: string; size: number; path: string }>; workspace: string | null }> {
  const formData = new FormData();
  files.forEach((file) => formData.append("files", file));
  if (instructions.trim()) formData.append("instructions", instructions.trim());

  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", apiUrl(API_BASE, `${boardFilesPath(taskId, "/upload")}`, "POST"));
    request.withCredentials = true;
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress?.(Math.round((event.loaded / event.total) * 100));
    });
    request.addEventListener("error", () => reject(new Error("Could not upload files.")));
    request.addEventListener("abort", () => reject(new Error("File upload was cancelled.")));
    request.addEventListener("load", () => {
      let body: { error?: { message?: string } } & Record<string, unknown> = {};
      try { body = JSON.parse(request.responseText) as typeof body; } catch { /* handled below */ }
      if (request.status < 200 || request.status >= 300) {
        reject(new CollabApiError(body.error?.message ?? request.statusText ?? "Could not upload files.", request.status));
        return;
      }
      resolve(body as unknown as { saved: Array<{ name: string; size: number; path: string }>; workspace: string | null });
    });
    request.send(formData);
  });
}

export const collabApi = {
  agents: () => USE_FIXTURES ? Promise.resolve(FIXTURE_AGENTS) : req<Agent[]>("/api/collab/agents"),
  // boardTasks() removed with GET /api/collab/board — a duplicate,
  // unauthenticated listing of the same table as GET /api/board, which no
  // screen called. Use liveBoard() (below) for the card list.
  /**
   * The LIVE board: each card reconciled from its linked run (GET /api/board).
   * `archived` mirrors the backend's `?archived=1` — default (false/undefined)
   * excludes archived cards, `true` returns ONLY archived ones (not a merge).
   * `project_id` narrows the server response when the enriched board route
   * supports it; the page also filters client-side as a belt-and-braces.
   */
  liveBoard: (params?: { archived?: boolean; project_id?: string }) => {
    const qs = new URLSearchParams();
    if (params?.archived) qs.set("archived", "1");
    if (params?.project_id) qs.set("project_id", params.project_id);
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return USE_FIXTURES
      ? Promise.resolve(
          FIXTURE_LIVE_TASKS
            .filter((task) => Boolean(task.archived) === Boolean(params?.archived))
            .filter((task) => !params?.project_id || task.project_id === params.project_id),
        )
      : req<LiveBoardTask[]>(`/api/board${suffix}`);
  },
  /**
   * ONE reconciled card (GET /api/board/{task_id}) — the same shape as an
   * element of `liveBoard()`. The detail panel used to find its card by
   * downloading the entire board (megabytes) and then, on a miss, the entire
   * archived feed as well. 404 means no such card; callers translate that into
   * their own fallback rather than a hard error.
   */
  boardCard: (taskId: string) => USE_FIXTURES
    ? (() => {
        const task = FIXTURE_LIVE_TASKS.find((item) => item.id === taskId);
        return task
          ? Promise.resolve<LiveBoardTask>(task)
          : Promise.reject(new CollabApiError("404: board task not found", 404));
      })()
    : req<LiveBoardTask>(`/api/board/${encodeURIComponent(taskId)}`),
  /**
   * Ranked Needs Response band (GET /api/board/needs-response).
   * Strict: only conversations that cannot proceed until the operator acts.
   * Ordering is the product — no client sort control.
   */
  needsResponse: async () => {
    if (!USE_FIXTURES) {
      return req<{ items: LiveBoardTask[]; count: number }>("/api/board/needs-response");
    }
    // Same strict rank as production — import the pure selector so fixtures
    // never disagree with the live band order.
    const { selectNeedsResponse } = await import("@/features/board/needsResponse");
    const items = selectNeedsResponse(
      FIXTURE_LIVE_TASKS.filter((task) => !task.archived),
    );
    return { items, count: items.length };
  },
  /** Archive a board card (POST /api/board/{task_id}/archive, empty body). */
  archiveTask: (taskId: string) => USE_FIXTURES
    ? (() => {
        const task = FIXTURE_LIVE_TASKS.find((item) => item.id === taskId);
        if (task) task.archived = true;
        return Promise.resolve<Record<string, unknown>>({ task_id: taskId, archived: true, paused_run: task?.paused_run ?? null, paused_session: task?.paused_session ?? null });
      })()
    : req<Record<string, unknown>>(`/api/board/${encodeURIComponent(taskId)}/archive`, { method: "POST", body: "{}" }),
  archiveTasks: (taskIds: string[]) => USE_FIXTURES
    ? (() => {
        const archived: string[] = [];
        const paused_runs: string[] = [];
        const paused_sessions: string[] = [];
        taskIds.forEach((taskId) => {
          const task = FIXTURE_LIVE_TASKS.find((item) => item.id === taskId);
          if (!task || task.archived) return;
          task.archived = true;
          archived.push(taskId);
          if (task.paused_run) paused_runs.push(task.paused_run);
          if (task.paused_session) paused_sessions.push(task.paused_session);
        });
        return Promise.resolve<ArchiveTasksResult>({ archived, skipped: [], paused_runs, paused_sessions });
      })()
    : req<ArchiveTasksResult>("/api/board/archive", { method: "POST", body: JSON.stringify({ task_ids: taskIds }) }),
  claimTask: (taskId: string, agentId: string = "operator") => USE_FIXTURES
    ? Promise.resolve<ClaimResult>({ success: true, claimed_by: "agt-1", claim_version: 1 })
    : req<ClaimResult>(`/api/collab/board/${encodeURIComponent(taskId)}/claim`, { method: "POST", body: JSON.stringify({ agent_id: agentId }) }),
  channels: () => USE_FIXTURES ? Promise.resolve(FIXTURE_CHANNELS) : req<Channel[]>("/api/collab/channels"),
  messages: (channelId: string) => USE_FIXTURES
    ? Promise.resolve(FIXTURE_MESSAGES.filter((message) => message.channel_id === channelId))
    : req<Message[]>(`/api/collab/channels/${encodeURIComponent(channelId)}/messages`),
  postMessage: (channelId: string, body: string, kind?: MessageKind, ref?: string) => USE_FIXTURES
    ? Promise.resolve<Message>({ id: `fixture-${Date.now()}`, channel_id: channelId, from_agent: "operator", to_agent: null, kind: kind ?? "message", body, ref: ref ?? null, ts: new Date().toISOString() })
    : req<Message>(`/api/collab/channels/${encodeURIComponent(channelId)}/messages`, { method: "POST", body: JSON.stringify({ from_agent: "operator", body, kind: kind ?? "message", ref }) }),
  searchMessages: (q: string) => USE_FIXTURES
    ? Promise.resolve(FIXTURE_MESSAGES.filter((message) => message.body.toLowerCase().includes(q.toLowerCase())))
    : req<Message[]>(`/api/collab/messages/search?q=${encodeURIComponent(q)}`),

  // W5 Longhaul endpoints
  fetchCategories: () => USE_FIXTURES
    ? Promise.resolve<TaskCategory[]>([])
    : req<TaskCategory[]>("/api/categories"),

  createCategory: (name: string, color?: string, wip_limit?: number) => USE_FIXTURES
    ? Promise.resolve<TaskCategory>({ id: `cat-${Date.now()}`, name, slug: name.toLowerCase().replace(/\s+/g, '-'), color: color ?? '', wip_limit: wip_limit ?? 1, created_at: new Date().toISOString(), updated_at: new Date().toISOString() })
    : req<TaskCategory>("/api/categories", { method: "POST", body: JSON.stringify({ name, color, wip_limit }) }),

  updateCategory: (id: string, updates: Partial<Omit<TaskCategory, 'id' | 'created_at' | 'updated_at'>>) => USE_FIXTURES
    ? Promise.resolve<TaskCategory>({ id, name: updates.name ?? '', slug: updates.slug ?? '', color: updates.color ?? '', wip_limit: updates.wip_limit ?? 1, created_at: new Date().toISOString(), updated_at: new Date().toISOString() })
    : req<TaskCategory>(`/api/categories/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(updates) }),

  fetchTaskConversation: (taskId: string, limit?: number) => USE_FIXTURES
    ? Promise.resolve<TaskTurn[]>([])
    : req<TaskTurn[]>(`/api/board/${encodeURIComponent(taskId)}/conversation${limit ? `?limit=${limit}` : ""}`),

  sendTaskMessage: (taskId: string, content: string) => USE_FIXTURES
    ? Promise.resolve<TaskTurn>({ id: `turn-${Date.now()}`, scope_type: 'task', scope_id: taskId, seq: 0, role: 'steering', content, created_at: new Date().toISOString() })
    : req<TaskTurn>(`/api/board/${encodeURIComponent(taskId)}/message`, { method: "POST", body: JSON.stringify({ content }) }),

  fetchLonghaulDetail: (taskId: string) => USE_FIXTURES
    ? Promise.resolve<LonghaulTaskDetail>({ attempts: [], workbook_content: '', acceptance: '', park_state: null })
    : req<LonghaulTaskDetail>(`/api/board/${encodeURIComponent(taskId)}/longhaul`),
  /** Unified session lookup for a card (active tasks included) — the
   * task-details page's source for "which models are working". */
  fetchTaskSessions: (taskId: string) => USE_FIXTURES
    ? Promise.resolve<BoardTaskSessions>({ sessions: [], orchestration: null, live_session_id: null })
    : req<BoardTaskSessions>(`/api/board/${encodeURIComponent(taskId)}/sessions`),
  /** Pause a card's live work without archiving (resumable via retryTask). */
  pauseTask: (taskId: string) => USE_FIXTURES
    ? Promise.resolve<Record<string, unknown>>({ task_id: taskId, status: "cancelled", resumable: true })
    : req<Record<string, unknown>>(`/api/board/${encodeURIComponent(taskId)}/pause`, { method: "POST", body: "{}" }),
  /** Resume a paused/failed/blocked card via orchestration resume. */
  retryTask: (taskId: string) => USE_FIXTURES
    ? Promise.resolve<Record<string, unknown>>({ task_id: taskId, resumed: true })
    : req<Record<string, unknown>>(`/api/board/${encodeURIComponent(taskId)}/retry`, { method: "POST", body: "{}" }),

  // Session ledger (~/.omniagentos/ops/bin/ledger relay — session-ledger integration
  // brief, 2026-08-04). Both routes are session-token-gated server-side; the
  // Next.js proxy attaches the token via a LITERAL two-path match in
  // isAuthorizedReadPath (app/api/[...path]/route.ts) -- NOT via
  // AUTHORIZED_READ_PREFIXES (a whole-prefix match would also gate the
  // pre-existing PUBLIC GET /api/ledger under the same prefix).
  /** One card's event timeline, newest-last. Fetched LAZY-ON-OPEN by
   * useTaskDetail — never for every card on the board. */
  fetchLedgerTail: (taskId: string, n = 50) => USE_FIXTURES
    ? Promise.resolve<LedgerEvent[]>([])
    : req<LedgerEvent[]>(`/api/ledger/tail?ref=${encodeURIComponent(`task=${taskId}`)}&n=${n}`),
  /** Every OPEN claim across every project — the board header's strip. */
  fetchLedgerClaims: () => USE_FIXTURES
    ? Promise.resolve<LedgerClaim[]>([])
    : req<LedgerClaim[]>("/api/ledger/claims"),
};
