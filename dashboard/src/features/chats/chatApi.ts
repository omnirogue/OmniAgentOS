/**
 * Chat API client — talks to the chat v2 backend with NEXT_PUBLIC_USE_CHATS_FIXTURES
 * fallback. Reads and mutations both go same-origin through the Next.js proxy
 * (SEC-005 convention, same as hierarchy.ts and collab/client.ts).
 *
 * Pinned contracts consumed (chat-v2 SPEC §3):
 *   GET  /api/chats                    -> ChatDTO[]
 *   GET  /api/chats/folders            -> { folders: [{name, color, chat_count}] }
 *   POST /api/chats/folders/{name}/rename -> { folder }  (moves member chats)
 *   POST /api/chats/folders/{name}/color  -> { folder }  (upsert = folder creation)
 *   DELETE /api/chats/folders/{name}      -> { deleted, chats_moved }
 *   POST /api/chats                    -> ChatDTO  (board_task_id links an existing card)
 *   GET  /api/chats/{id}               -> ChatDTO
 *   PATCH /api/chats/{id}              -> ChatDTO  (model/orch_mode/plan_mode/routing/project_id)
 *   DELETE /api/chats/{id}             -> ChatDTO
 *   GET  /api/chats/{id}/messages      -> ChatMessage[]
 *   POST /api/chats/{id}/messages      -> SendResult { message, dispatch } | { message, task_ids }
 *   POST /api/chats/{id}/classify      -> ProjectSuggestion
 *   POST /api/chats/{id}/plan          -> { job_id, status }
 *   POST /api/chats/{id}/spawn         -> { task_ids: string[] }
 *   POST /api/chats/{id}/promote       -> { project_id: string, task_ids: string[] }
 *   GET  /api/models                   -> { models: ModelEntry[] }
 *   GET  /api/skills/tree              -> SkillTreeEntry[]
 *   POST /api/board/{task_id}/files/upload -> { saved: [...] }
 */

import { fetchWithTimeout } from "@/lib/fetchTimeout";
import {
  parseFoldersResponse,
  type FolderColor,
  type FolderInfo,
} from "./folders";

export const USE_CHAT_FIXTURES =
  process.env.NEXT_PUBLIC_USE_CHATS_FIXTURES === "true";

// ── Types ─────────────────────────────────────────────────────

export type ChatStatus = "active" | "archived" | "promoted" | "deleted";
export type OrchMode = "auto" | "solo" | "fanout";
export type RoutingSpeed = "fast" | "auto" | "ultra" | null;
export type RoutingEffort = "low" | "medium" | "high" | null;

export interface RoutingPrefs {
  allow: string[];
  deny: string[];
  speed: RoutingSpeed;
  effort: RoutingEffort;
  hint: string | null;
}

export interface ProjectSuggestion {
  project_id: string | null;
  name: string | null;
  confidence: number;
  rationale: string;
}

/** The pinned ChatDTO (§3.1): flat fields are authoritative; meta is retained. */
export interface Chat {
  id: string;
  title: string;
  status: ChatStatus;
  project_id: string | null;
  project_name: string | null;
  board_task_id: string | null;
  preferred_model: string | null;
  orch_mode: OrchMode;
  plan_mode: boolean;
  routing: RoutingPrefs;
  project_suggestion: ProjectSuggestion | null;
  message_count: number;
  last_message_at: string | null;
  promoted_at: string | null;
  created_at: string;
  updated_at: string;
  meta: Record<string, unknown>;
}

export interface ChatCreateRequest {
  title?: string;
  project_id?: string | null;
  model?: string | null;
  board_task_id?: string | null;
  meta?: Record<string, unknown>;
}

export interface ChatPatchRequest {
  title?: string;
  status?: ChatStatus;
  project_id?: string | null;
  model?: string | null;
  orch_mode?: OrchMode;
  plan_mode?: boolean;
  routing?: Partial<RoutingPrefs>;
  folder?: string | null;
  meta?: Record<string, unknown>;
}

export interface Attachment {
  kind: "skill" | "file" | "url";
  ref: string;
  label: string;
}

export interface WorkFolderTreeNode {
  name: string;
  path: string;
  children: WorkFolderTreeNode[];
}

export interface WorkFolderTree {
  root: string;
  max_depth: number;
  entries: WorkFolderTreeNode[];
}

/**
 * Scope fields added by the composer's ctx-row.
 *
 * The company axis was ratified OUT of the chat surface and the intent fields
 * went with the shadow intent-suggestion row, so exactly one scope key is
 * written from the UI now. `meta` on the wire stays an open dict, so messages
 * persisted with the older keys keep round-tripping untouched.
 */
export interface ChatComposerMeta {
  work_folder: string | null;
}

export interface ChatMessage {
  id: string;
  seq: number;
  role: "user" | "agent" | "system";
  content: string;
  model: string | null;
  created_at: string;
  meta?: { attachments?: Attachment[]; session_id?: string; turn?: number; timed_out?: boolean } & Record<string, unknown>;
}

export interface ChatSendRequest {
  content: string;
  model?: string | null;
  orch_mode?: OrchMode | null;
  count?: number;
  meta?: { attachments?: Attachment[] } & Partial<ChatComposerMeta> & Record<string, unknown>;
}

/** The pinned send envelope (§3.4, fixes P0-4): the server shape is correct;
 * the client reconciles the optimistic user message by message.id and hands
 * dispatch.session_id to turn state. */
export interface SendResult {
  message: ChatMessage;
  dispatch?: {
    session_id: string | null;
    task_id: string | null;
    run_id: string | null;
    board_task: unknown;
    steered: boolean;
  };
  task_ids?: string[];
}

export interface ModelEntry {
  id: string;
  label: string;
  provider: string;
  tier: number | null;
  available: boolean;
  lineage: string | null;
  unavailable_reason?: string | null;
}

export interface SpawnResponse {
  task_ids: string[];
}

export interface PromoteResponse {
  project_id: string;
  task_ids: string[];
}

export interface ChatTurnEvent {
  type:
    | "chat.turn.started"
    | "chat.turn.delta"
    | "chat.turn.completed";
  chat_id: string;
  task_id: string;
  turn: number;
  text?: string;
  model?: string;
  timed_out?: boolean;
  error?: string;
  ts: string;
}

export interface SkillTreeEntry {
  id: string;
  name: string;
  version: string;
  path: string;
  description?: string | null;
}

export interface PlanSeedResponse {
  job_id: string;
  status: string;
}

export interface PlanJobStatus {
  job_id: string;
  status: "running" | "ready" | "error" | "confirmed";
  plan: {
    project_name?: string;
    description?: string;
    tasks?: Array<{ title?: string; description?: string }>;
    sub_projects?: Array<{ name?: string; tasks?: Array<{ title?: string }> }>;
  } | null;
  route: { decision?: string; project_name?: string } | null;
  route_target_name: string | null;
  error: string | null;
}

export interface EtaResponse {
  estimate_seconds: number | null;
  basis: "run_steps" | "session_progress" | "discipline_history" | null;
  sample_size: number;
  confidence: "low" | "medium" | "high" | null;
  computed_at: string;
}

// ── Error ─────────────────────────────────────────────────────

export class ChatApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ChatApiError";
  }
}

// ── Fetch helpers ─────────────────────────────────────────────

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const isForm = init?.body instanceof FormData;
  const res = await fetchWithTimeout(path, {
    ...init,
    headers: isForm
      ? { ...(init?.headers ?? {}) }
      : { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { error?: { message?: string } };
      detail = body?.error?.message ?? detail;
    } catch {
      /* keep statusText */
    }
    throw new ChatApiError(detail, res.status);
  }
  return (await res.json()) as T;
}

async function mutate<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  return fetchJson<T>(path, init);
}

const enc = encodeURIComponent;

// ── Fixtures ──────────────────────────────────────────────────

const now = Date.now();
const ago = (min: number) => new Date(now - min * 60_000).toISOString();

const DEFAULT_ROUTING: RoutingPrefs = {
  allow: [],
  deny: [],
  speed: null,
  effort: null,
  hint: null,
};

const FIXTURE_MODELS: ModelEntry[] = [
  {
    id: "auto",
    label: "Auto — router decides",
    provider: "router",
    tier: null,
    available: true,
    lineage: null,
  },
  {
    id: "grok-4.5",
    label: "Grok 4.5",
    provider: "xai",
    tier: 4,
    available: true,
    lineage: "grok",
  },
  {
    id: "grok-4",
    label: "Grok 4",
    provider: "xai",
    tier: 3,
    available: true,
    lineage: "grok",
  },
  {
    id: "sonnet-4",
    label: "Claude Sonnet 4",
    provider: "anthropic",
    tier: 3,
    available: true,
    lineage: "claude",
  },
  {
    id: "gemini-2.5-pro",
    label: "Gemini 2.5 Pro",
    provider: "google",
    tier: 3,
    available: true,
    lineage: "gemini",
  },
  {
    id: "gpt-4.1",
    label: "GPT-4.1",
    provider: "openai",
    tier: 3,
    available: false,
    lineage: "gpt",
    unavailable_reason: "No API key configured",
  },
];

function fixtureChat(partial: Partial<Chat> & { id: string; title: string }): Chat {
  return {
    status: "active",
    project_id: null,
    project_name: null,
    board_task_id: `tsk_${partial.id}`,
    preferred_model: null,
    orch_mode: "solo",
    plan_mode: false,
    routing: { ...DEFAULT_ROUTING },
    project_suggestion: null,
    message_count: 0,
    last_message_at: null,
    promoted_at: null,
    created_at: ago(60),
    updated_at: ago(5),
    meta: {},
    ...partial,
  };
}

const FIXTURE_CHATS: Chat[] = [
  fixtureChat({
    id: "chat_001",
    title: "Architecture review for API layer",
    project_id: "prj_platform",
    project_name: "Platform",
    message_count: 8,
    last_message_at: ago(10),
    created_at: ago(120),
    updated_at: ago(5),
  }),
  fixtureChat({
    id: "chat_002",
    title: "Dashboard redesign brainstorm",
    project_id: "prj_platform",
    project_name: "Platform",
    preferred_model: "grok-4",
    message_count: 14,
    last_message_at: ago(65),
    created_at: ago(360),
    updated_at: ago(60),
  }),
  fixtureChat({
    id: "chat_003",
    title: "Quick question about cascade routing",
    message_count: 3,
    last_message_at: ago(3),
    created_at: ago(12),
    updated_at: ago(2),
    project_suggestion: {
      project_id: "prj_platform",
      name: "Platform",
      confidence: 0.82,
      rationale: "mentions cascade routing and the API layer",
    },
  }),
];

const FIXTURE_MESSAGES: Record<string, ChatMessage[]> = {
  chat_001: [
    {
      id: "msg_001_1",
      seq: 1,
      role: "user",
      content:
        "Let's review the FastAPI routes — I think we can consolidate some endpoints.",
      model: null,
      created_at: ago(120),
      meta: {},
    },
    {
      id: "msg_001_2",
      seq: 2,
      role: "agent",
      content:
        "I see three groups that could be merged: the session endpoints, the run transcript endpoints, and the board CRUD routes. Each has overlapping auth and pagination patterns.",
      model: "grok-4",
      created_at: ago(118),
      meta: {},
    },
    {
      id: "msg_001_3",
      seq: 3,
      role: "user",
      content: "Good catch. Start with the session group — show me the diff.",
      model: null,
      created_at: ago(115),
      meta: {},
    },
  ],
  chat_002: [
    {
      id: "msg_002_1",
      seq: 1,
      role: "user",
      content: "Sketch out a sidebar layout for the chats page. Two-pane, not three.",
      model: null,
      created_at: ago(360),
      meta: {},
    },
    {
      id: "msg_002_2",
      seq: 2,
      role: "agent",
      content:
        "A 2-pane layout with a collapsible sidebar on the left (project tree + recent chats) and the thread view occupying the main area. Workspace details go in a right drawer that slides in on demand — never a permanent third column.",
      model: "grok-4",
      created_at: ago(358),
      meta: {},
    },
  ],
  chat_003: [
    {
      id: "msg_003_1",
      seq: 1,
      role: "user",
      content: "How does cascade routing pick between models?",
      model: null,
      created_at: ago(12),
      meta: {},
    },
  ],
};

const FIXTURE_SKILLS: SkillTreeEntry[] = [
  {
    id: "skill_pdf",
    name: "pdf",
    version: "1.2.0",
    path: "/skills/pdf",
    description: "Parse, extract, and transform PDF documents.",
  },
  {
    id: "skill_xlsx",
    name: "xlsx",
    version: "1.0.3",
    path: "/skills/xlsx",
    description: "Read and write Excel spreadsheets.",
  },
  {
    id: "skill_dataviz",
    name: "dataviz",
    version: "2.1.0",
    path: "/skills/dataviz",
    description: "Design guidance for charts, graphs, and dashboards.",
  },
  {
    id: "skill_new_app",
    name: "new-app",
    version: "1.0.0",
    path: "/skills/new-app",
    description: "Workflow for creating new applications from scratch.",
  },
  {
    id: "skill_review",
    name: "review",
    version: "1.4.0",
    path: "/skills/review",
    description: "Review changed code for correctness and quality.",
  },
];

const FIXTURE_WORK_FOLDER_TREE: WorkFolderTree = {
  root: "/Users/demo/Work",
  max_depth: 3,
  entries: [
    {
      name: "Acme",
      path: "Acme",
      children: [
        { name: "Product", path: "Acme/Product", children: [] },
      ],
    },
  ],
};

let fixtureSeq = 100;

function fixtureMsg(
  chatId: string,
  role: ChatMessage["role"],
  content: string,
  model: string | null,
  meta?: ChatMessage["meta"],
): ChatMessage {
  fixtureSeq += 1;
  return {
    id: `fxmsg_${fixtureSeq}`,
    seq: fixtureSeq,
    role,
    content,
    model,
    created_at: new Date().toISOString(),
    meta: meta ?? {},
  };
}

// ── Public API ────────────────────────────────────────────────

export async function listChats(projectId?: string | null): Promise<Chat[]> {
  if (USE_CHAT_FIXTURES) {
    const list = FIXTURE_CHATS.filter((c) => c.status !== "deleted");
    if (projectId === "") return list.filter((c) => !c.project_id);
    if (projectId) return list.filter((c) => c.project_id === projectId);
    return list;
  }
  // "" means "project-less" (the Recents bucket). The server's project_id
  // param is an exact match, so sending "" would match nothing — fetch
  // unfiltered and narrow client-side, mirroring the fixture behavior.
  if (projectId === "") {
    const all = await fetchJson<Chat[]>("/api/chats");
    return all.filter((c) => !c.project_id);
  }
  const qs = projectId !== undefined && projectId !== null
    ? `?project_id=${enc(projectId)}`
    : "";
  return fetchJson<Chat[]>(`/api/chats${qs}`);
}

// ── Folder registry (088) ─────────────────────────────────────

/** In-memory fixture registry, mirroring chat_folders upsert semantics. */
const FIXTURE_FOLDERS: FolderInfo[] = [];

function fixtureFolderList(): FolderInfo[] {
  const counts = new Map<string, number>();
  for (const chat of FIXTURE_CHATS) {
    if (chat.status === "deleted") continue;
    const folder =
      typeof chat.meta?.folder === "string" ? chat.meta.folder.trim() : "";
    if (folder) counts.set(folder, (counts.get(folder) ?? 0) + 1);
  }
  const seen = new Set<string>();
  const out: FolderInfo[] = [];
  for (const info of FIXTURE_FOLDERS) {
    seen.add(info.name);
    out.push({ ...info, chat_count: counts.get(info.name) ?? 0 });
  }
  const unregistered = [...counts.keys()]
    .filter((name) => !seen.has(name))
    .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  for (const name of unregistered) {
    out.push({ name, color: "gray", chat_count: counts.get(name) ?? 0 });
  }
  return out;
}

/** Folder registry: union of registered folders and folders on live chats. */
export async function listFolders(): Promise<FolderInfo[]> {
  if (USE_CHAT_FIXTURES) return fixtureFolderList();
  const data = await fetchJson<unknown>("/api/chats/folders");
  return parseFoldersResponse(data);
}

/** Set (or register) a folder's color token — also the new-folder path. */
export async function setFolderColor(
  name: string,
  color: FolderColor,
): Promise<FolderInfo> {
  const trimmed = name.trim();
  if (USE_CHAT_FIXTURES) {
    const existing = FIXTURE_FOLDERS.find((f) => f.name === trimmed);
    if (existing) existing.color = color;
    else FIXTURE_FOLDERS.push({ name: trimmed, color, chat_count: 0 });
    return fixtureFolderList().find((f) => f.name === trimmed)!;
  }
  const data = await mutate<{ folder: unknown }>(
    `/api/chats/folders/${enc(trimmed)}/color`,
    { method: "POST", body: JSON.stringify({ color }) },
  );
  return parseFoldersResponse({ folders: [data.folder] })[0];
}

/** Rename a folder — every member chat moves; renaming onto an existing
 * folder merges into it (the target keeps its color). */
export async function renameFolder(
  name: string,
  newName: string,
): Promise<FolderInfo> {
  const src = name.trim();
  const dst = newName.trim();
  if (USE_CHAT_FIXTURES) {
    for (const chat of FIXTURE_CHATS) {
      if (
        typeof chat.meta?.folder === "string" &&
        chat.meta.folder.trim() === src
      ) {
        chat.meta = { ...chat.meta, folder: dst };
      }
    }
    const srcIdx = FIXTURE_FOLDERS.findIndex((f) => f.name === src);
    if (srcIdx >= 0) {
      if (FIXTURE_FOLDERS.some((f) => f.name === dst)) {
        FIXTURE_FOLDERS.splice(srcIdx, 1);
      } else {
        FIXTURE_FOLDERS[srcIdx] = { ...FIXTURE_FOLDERS[srcIdx], name: dst };
      }
    }
    return (
      fixtureFolderList().find((f) => f.name === dst) ?? {
        name: dst,
        color: "gray",
        chat_count: 0,
      }
    );
  }
  const data = await mutate<{ folder: unknown }>(
    `/api/chats/folders/${enc(src)}/rename`,
    { method: "POST", body: JSON.stringify({ new_name: dst }) },
  );
  return parseFoldersResponse({ folders: [data.folder] })[0];
}

/** Delete a folder — member chats fall back to the Inbox (folder cleared). */
export async function deleteFolder(
  name: string,
): Promise<{ deleted: string; chats_moved: number }> {
  const trimmed = name.trim();
  if (USE_CHAT_FIXTURES) {
    let moved = 0;
    for (const chat of FIXTURE_CHATS) {
      if (
        typeof chat.meta?.folder === "string" &&
        chat.meta.folder.trim() === trimmed
      ) {
        const rest = { ...chat.meta };
        delete rest.folder;
        chat.meta = rest;
        moved += 1;
      }
    }
    const idx = FIXTURE_FOLDERS.findIndex((f) => f.name === trimmed);
    if (idx >= 0) FIXTURE_FOLDERS.splice(idx, 1);
    return { deleted: trimmed, chats_moved: moved };
  }
  return mutate<{ deleted: string; chats_moved: number }>(
    `/api/chats/folders/${enc(trimmed)}`,
    { method: "DELETE" },
  );
}

export async function createChat(
  req: ChatCreateRequest,
): Promise<Chat> {
  if (USE_CHAT_FIXTURES) {
    fixtureSeq += 1;
    const chat = fixtureChat({
      id: `fxchat_${fixtureSeq}`,
      title: req.title ?? "New chat",
      project_id: req.project_id ?? null,
      preferred_model: req.model ?? null,
      board_task_id: req.board_task_id ?? null,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
    FIXTURE_CHATS.push(chat);
    return chat;
  }
  return mutate<Chat>("/api/chats", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function getChat(chatId: string): Promise<Chat> {
  if (USE_CHAT_FIXTURES) {
    const chat = FIXTURE_CHATS.find((c) => c.id === chatId);
    if (!chat) throw new ChatApiError("Chat not found", 404);
    return chat;
  }
  return fetchJson<Chat>(`/api/chats/${enc(chatId)}`);
}

export async function updateChat(
  chatId: string,
  patch: ChatPatchRequest,
): Promise<Chat> {
  if (USE_CHAT_FIXTURES) {
    const idx = FIXTURE_CHATS.findIndex((c) => c.id === chatId);
    if (idx < 0) throw new ChatApiError("Chat not found", 404);
    const current = FIXTURE_CHATS[idx];
    FIXTURE_CHATS[idx] = {
      ...current,
      ...(patch.title !== undefined ? { title: patch.title } : {}),
      ...(patch.status !== undefined ? { status: patch.status } : {}),
      ...(patch.project_id !== undefined ? { project_id: patch.project_id } : {}),
      ...(patch.model !== undefined ? { preferred_model: patch.model } : {}),
      ...(patch.orch_mode !== undefined ? { orch_mode: patch.orch_mode } : {}),
      ...(patch.plan_mode !== undefined ? { plan_mode: patch.plan_mode } : {}),
      ...(patch.routing !== undefined
        ? { routing: { ...current.routing, ...patch.routing } }
        : {}),
      ...(patch.meta !== undefined
        ? { meta: { ...current.meta, ...patch.meta } }
        : {}),
      updated_at: new Date().toISOString(),
    };
    return FIXTURE_CHATS[idx];
  }
  return mutate<Chat>(`/api/chats/${enc(chatId)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export async function deleteChat(chatId: string): Promise<void> {
  if (USE_CHAT_FIXTURES) {
    const idx = FIXTURE_CHATS.findIndex((c) => c.id === chatId);
    if (idx >= 0) FIXTURE_CHATS[idx] = { ...FIXTURE_CHATS[idx], status: "deleted" };
    return;
  }
  await mutate<Chat>(`/api/chats/${enc(chatId)}`, {
    method: "DELETE",
  });
}

export async function listMessages(
  chatId: string,
): Promise<ChatMessage[]> {
  if (USE_CHAT_FIXTURES) {
    return [...(FIXTURE_MESSAGES[chatId] ?? [])];
  }
  return fetchJson<ChatMessage[]>(
    `/api/chats/${enc(chatId)}/messages`,
  );
}

/**
 * The workfs API currently exposes one bounded tree (max depth 3). Consumers
 * call this only after an operator asks to browse work folders.
 */
export async function fetchWorkFolderTree(): Promise<WorkFolderTree> {
  if (USE_CHAT_FIXTURES) return FIXTURE_WORK_FOLDER_TREE;
  return fetchJson<WorkFolderTree>("/api/workfs/tree");
}

export async function sendMessage(
  chatId: string,
  req: ChatSendRequest,
): Promise<SendResult> {
  if (USE_CHAT_FIXTURES) {
    const msg = fixtureMsg(chatId, "user", req.content, req.model ?? null, req.meta);
    (FIXTURE_MESSAGES[chatId] ??= []).push(msg);
    if (req.orch_mode === "fanout") {
      return {
        message: msg,
        task_ids: Array.from(
          { length: req.count ?? 1 },
          (_, i) => `fxspawn_${Date.now()}_${i}`,
        ),
      };
    }
    // Simulate the agent reply landing shortly after (fixture mode has no bridge).
    window.setTimeout(() => {
      (FIXTURE_MESSAGES[chatId] ??= []).push(
        fixtureMsg(
          chatId,
          "agent",
          `Fixture reply to: “${req.content.slice(0, 80)}”`,
          req.model ?? "grok-4",
          { session_id: "fx_session" },
        ),
      );
    }, 800);
    return {
      message: msg,
      dispatch: {
        session_id: "fx_session",
        task_id: null,
        run_id: null,
        board_task: null,
        steered: false,
      },
    };
  }
  return mutate<SendResult>(`/api/chats/${enc(chatId)}/messages`, {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function classifyChat(chatId: string, force = false): Promise<ProjectSuggestion> {
  if (USE_CHAT_FIXTURES) {
    return (
      FIXTURE_CHATS.find((c) => c.id === chatId)?.project_suggestion ?? {
        project_id: null,
        name: null,
        confidence: 0,
        rationale: "",
      }
    );
  }
  return mutate<ProjectSuggestion>(`/api/chats/${enc(chatId)}/classify`, {
    method: "POST",
    body: JSON.stringify(force ? { force: true } : {}),
  });
}

export async function seedPlan(
  chatId: string,
  goal?: string | null,
  speed?: RoutingSpeed,
): Promise<PlanSeedResponse> {
  if (USE_CHAT_FIXTURES) {
    return { job_id: `fxjob_${Date.now()}`, status: "running" };
  }
  return mutate<PlanSeedResponse>(`/api/chats/${enc(chatId)}/plan`, {
    method: "POST",
    body: JSON.stringify({ goal: goal ?? null, speed: speed ?? null }),
  });
}

export async function fetchPlanJob(jobId: string): Promise<PlanJobStatus> {
  if (USE_CHAT_FIXTURES) {
    return {
      job_id: jobId,
      status: "ready",
      plan: {
        project_name: "Fixture Plan",
        description: "A planned project from the fixture planner.",
        tasks: [{ title: "First task" }, { title: "Second task" }],
      },
      route: { decision: "new", project_name: "Fixture Plan" },
      route_target_name: "Fixture Plan",
      error: null,
    };
  }
  return fetchJson<PlanJobStatus>(`/api/intake/plan/${enc(jobId)}`);
}

export async function confirmPlanJob(
  jobId: string,
  projectOverride?: string,
  speed?: RoutingSpeed,
): Promise<Record<string, unknown>> {
  if (USE_CHAT_FIXTURES) {
    return { confirmed: true, job_id: jobId };
  }
  return mutate<Record<string, unknown>>(`/api/intake/plan/${enc(jobId)}/confirm`, {
    method: "POST",
    body: JSON.stringify({
      project_override: projectOverride ?? "auto",
      speed: speed ?? null,
    }),
  });
}

export async function spawnChat(
  chatId: string,
  goal: string,
  count?: number,
): Promise<SpawnResponse> {
  if (USE_CHAT_FIXTURES) {
    return {
      task_ids: Array.from(
        { length: count ?? 2 },
        (_, i) => `fxspawn_${Date.now()}_${i}`,
      ),
    };
  }
  return mutate<SpawnResponse>(
    `/api/chats/${enc(chatId)}/spawn`,
    {
      method: "POST",
      body: JSON.stringify({ goal, count }),
    },
  );
}

export async function promoteChat(
  chatId: string,
  projectId: string | null,
  newProjectTitle?: string,
): Promise<PromoteResponse> {
  if (USE_CHAT_FIXTURES) {
    return {
      project_id:
        projectId ?? `fxprj_${Date.now()}`,
      task_ids: [
        `fxtask_${Date.now()}_1`,
        `fxtask_${Date.now()}_2`,
      ],
    };
  }
  return mutate<PromoteResponse>(
    `/api/chats/${enc(chatId)}/promote`,
    {
      method: "POST",
      body: JSON.stringify({
        project_id: projectId,
        new_project_title: newProjectTitle,
      }),
    },
  );
}

export async function fetchModels(): Promise<ModelEntry[]> {
  if (USE_CHAT_FIXTURES) return [...FIXTURE_MODELS];
  const data = await fetchJson<{ models: ModelEntry[] }>(
    "/api/models",
  );
  return data.models;
}

export async function fetchSkillsTree(): Promise<SkillTreeEntry[]> {
  if (USE_CHAT_FIXTURES) return [...FIXTURE_SKILLS];
  return fetchJson<SkillTreeEntry[]>("/api/skills/tree");
}

export async function uploadChatFile(
  taskId: string,
  files: File[],
): Promise<{
  saved: Array<{ name: string; size: number; path: string }>;
  workspace: string | null;
}> {
  const formData = new FormData();
  files.forEach((f) => formData.append("files", f));
  if (USE_CHAT_FIXTURES) {
    return {
      saved: files.map((f) => ({
        name: f.name,
        size: f.size,
        path: `uploads/${f.name}`,
      })),
      workspace: null,
    };
  }
  return fetchJson(
    `/api/board/${enc(taskId)}/files/upload`,
    { method: "POST", body: formData },
  );
}

/** Create a project (sidebar + button). POST /api/projects. */
export async function createProject(name: string): Promise<{ id: string; name: string }> {
  if (USE_CHAT_FIXTURES) {
    return { id: `fxprj_${Date.now()}`, name };
  }
  return mutate<{ id: string; name: string }>("/api/projects", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

/** ETA for one board card (§3.10). Null estimate → "Estimating…". */
export async function fetchBoardEta(taskId: string): Promise<EtaResponse> {
  if (USE_CHAT_FIXTURES) {
    return {
      estimate_seconds: 420,
      basis: "session_progress",
      sample_size: 5,
      confidence: "low",
      computed_at: new Date().toISOString(),
    };
  }
  return fetchJson<EtaResponse>(`/api/board/${enc(taskId)}/eta`);
}

/** Children of one board card (drawer sub-task disclosure, §3.9). */
export async function fetchBoardChildren(taskId: string): Promise<Array<Record<string, unknown>>> {
  if (USE_CHAT_FIXTURES) return [];
  return fetchJson<Array<Record<string, unknown>>>(
    `/api/board?parent_task_id=${enc(taskId)}`,
  );
}
