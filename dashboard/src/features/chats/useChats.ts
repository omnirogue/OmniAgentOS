"use client";

/**
 * React hooks for the Chat v2 feature.
 *
 * Every hook consumes the chatApi client and follows the project's fixture
 * convention: when NEXT_PUBLIC_USE_CHATS_FIXTURES=true (or the API is
 * unreachable), the hook falls back to in-memory fixture data so the page
 * always renders. `source` lets the UI show a "demo data" notice.
 *
 * Hooks:
 *   useChatList      — chat list (ChatDTOs)
 *   useChatFolders   — folder registry with colors (088)
 *   useChatThread    — messages + streaming + deck state for one chat
 *   useSkillsForMention — skill tree for @mention autocomplete
 *
 * The thread hook is ONE useReducer (chat-v2 §P0-2 fix contract) with the
 * 6s-no-delta poll fallback (§P0-1: bridge failure degrades to slow, never
 * silent) and a module-level message cache that survives /chats ↔ /board
 * navigation (§2.1).
 */

import {
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
} from "react";
import {
  USE_CHAT_FIXTURES,
  type Attachment,
  type ChatComposerMeta,
  type Chat,
  type ChatMessage,
  type ChatPatchRequest,
  type ChatTurnEvent,
  type OrchMode,
  type PlanJobStatus,
  type RoutingSpeed,
  type SkillTreeEntry,
  type SpawnResponse,
  type PromoteResponse,
} from "./chatApi";
import * as chatApi from "./chatApi";
import type { FolderInfo } from "./folders";
import { useEventChannel } from "@/lib/useEventChannel";

export type DataSource = "live" | "fixtures";

function errText(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

// ── useChatList ───────────────────────────────────────────────

interface ChatListState {
  chats: Chat[];
  loading: boolean;
  error: string | null;
  source: DataSource;
}

export type ChatListActions = {
  refresh: () => Promise<void>;
};

export function useChatList(projectId?: string | null): ChatListState & ChatListActions {
  const [state, setState] = useState<ChatListState>({
    chats: [],
    loading: true,
    error: null,
    source: "live",
  });

  const refresh = useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true }));
    try {
      const chats = await chatApi.listChats(projectId);
      setState({
        chats,
        loading: false,
        error: null,
        source: USE_CHAT_FIXTURES ? "fixtures" : "live",
      });
    } catch (reason) {
      setState((prev) => ({
        ...prev,
        loading: false,
        error: errText(reason, "Could not load chats"),
        source: "fixtures",
      }));
    }
  }, [projectId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { ...state, refresh };
}

// ── useChatFolders (088 folder registry: name + color + count) ─

export function useChatFolders() {
  const [folders, setFolders] = useState<FolderInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const result = await chatApi.listFolders();
      setFolders(result);
      setError(null);
    } catch (reason) {
      setError(errText(reason, "Could not load folders"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { folders, loading, error, refresh };
}

// ── Module-level thread cache (§2.1) ──────────────────────────

interface CacheEntry {
  messages: ChatMessage[];
  fetchedAt: number;
}

const threadCache = new Map<string, CacheEntry>();
const CACHE_FRESH_MS = 30_000;

/** Warm/readable synchronously: selectChat renders from cache, revalidates. */
export function cachedThread(chatId: string): ChatMessage[] | null {
  return threadCache.get(chatId)?.messages ?? null;
}

function cacheThread(chatId: string, messages: ChatMessage[]): void {
  threadCache.set(chatId, { messages, fetchedAt: Date.now() });
}

// ── useChatThread ─────────────────────────────────────────────

export type TurnState =
  | "idle"
  | "queued"
  | "running"
  | "stalled"
  | "completed"
  | "timed_out";

/** The last frame the client actually received for a turn — surfaced by the
 * composer's activity strip so a quiet turn reads as "waiting on the bridge"
 * rather than "broken". */
export type TurnEventKind = "started" | "delta" | "completed" | "poll";

interface TurnSlice {
  state: TurnState;
  text: string;
  model: string | null;
  turnSeq: number | null;
  sessionId: string | null;
  startedAt: number | null;
  lastDeltaAt: number | null;
  lastEventType: TurnEventKind | null;
  fanoutTaskIds: string[] | null;
}

interface ThreadState {
  chat: Chat | null;
  messages: ChatMessage[];
  loading: boolean;
  error: string | null;
  sending: boolean;
  turn: TurnSlice;
  suggestionDismissed: boolean;
}

type ThreadAction =
  | { type: "LOADING" }
  | { type: "LOADED"; chat: Chat; messages: ChatMessage[] }
  | { type: "ERROR"; error: string }
  | { type: "CHAT_UPDATED"; chat: Chat }
  | { type: "SENT"; optimistic: ChatMessage }
  | { type: "SEND_DONE"; optimisticId: string; message: ChatMessage; sessionId: string | null; fanoutTaskIds: string[] | null }
  | { type: "SEND_FAILED"; optimisticId: string; error: string }
  | { type: "TURN_STARTED"; model: string | null; turn: number | null }
  | { type: "DELTA"; text: string; turn: number | null }
  | { type: "COMPLETED"; text: string; model: string | null; turn: number | null; sessionId: string | null; timedOut: boolean; error: string | null }
  | { type: "STALLED" }
  | { type: "POLL_MESSAGES"; messages: ChatMessage[] }
  | { type: "DISMISS_SUGGESTION" }
  | { type: "RESET" };

const IDLE_TURN: TurnSlice = {
  state: "idle",
  text: "",
  model: null,
  turnSeq: null,
  sessionId: null,
  startedAt: null,
  lastDeltaAt: null,
  lastEventType: null,
  fanoutTaskIds: null,
};

function dedupeMessages(msgs: ChatMessage[]): ChatMessage[] {
  const seen = new Map<string, ChatMessage>();
  for (const m of msgs) seen.set(m.id, m);
  return [...seen.values()].sort((a, b) => a.seq - b.seq);
}

/**
 * Merge a polled server list into current state. Server rows win by id, but a
 * wholesale REPLACE can drop messages that only exist client-side — the
 * optimistic user message (slow POST) or the SSE-built agent reply (poll
 * raced the bridge's persist). Provisional copies (the "optimistic_" and
 * "sse_" id prefixes) are dropped once their server twin (same session_id +
 * turn) arrives.
 */
function mergePolledMessages(current: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
  const serverTurnKeys = new Set(
    incoming
      .filter((m) => typeof m.meta?.session_id === "string")
      .map((m) => `${m.meta?.session_id as string}:${m.meta?.turn ?? ""}`),
  );
  const kept = current.filter((m) => {
    const provisional = m.id.startsWith("optimistic_") || m.id.startsWith("sse_");
    if (!provisional) return true;
    const key =
      typeof m.meta?.session_id === "string"
        ? `${m.meta.session_id}:${m.meta.turn ?? ""}`
        : null;
    return !(key && serverTurnKeys.has(key));
  });
  return dedupeMessages([...kept, ...incoming]);
}

function reducer(state: ThreadState, action: ThreadAction): ThreadState {
  switch (action.type) {
    case "LOADING":
      return { ...state, loading: true };
    case "LOADED":
      return {
        ...state,
        chat: action.chat,
        messages: action.messages,
        loading: false,
        error: null,
        sending: false,
        turn: IDLE_TURN,
        suggestionDismissed: false,
      };
    case "ERROR":
      return { ...state, loading: false, sending: false, error: action.error };
    case "CHAT_UPDATED":
      return { ...state, chat: action.chat };
    case "SENT":
      return {
        ...state,
        sending: true,
        error: null,
        messages: dedupeMessages([...state.messages, action.optimistic]),
        turn: { ...IDLE_TURN, state: "queued", startedAt: Date.now() },
      };
    case "SEND_DONE": {
      const messages = dedupeMessages(
        state.messages.map((m) => (m.id === action.optimisticId ? action.message : m)),
      );
      if (action.fanoutTaskIds) {
        return {
          ...state,
          sending: false,
          messages,
          turn: { ...IDLE_TURN, state: "completed", fanoutTaskIds: action.fanoutTaskIds },
        };
      }
      return {
        ...state,
        sending: false,
        messages,
        turn: {
          ...state.turn,
          // The started SSE may beat the POST response; keep it running.
          state: state.turn.state === "running" || state.turn.state === "stalled" ? state.turn.state : "running",
          sessionId: action.sessionId ?? state.turn.sessionId,
        },
      };
    }
    case "SEND_FAILED":
      return {
        ...state,
        sending: false,
        error: action.error,
        messages: state.messages.filter((m) => m.id !== action.optimisticId),
        turn: IDLE_TURN,
      };
    case "TURN_STARTED":
      return {
        ...state,
        turn: {
          ...IDLE_TURN,
          state: "running",
          model: action.model,
          turnSeq: action.turn,
          sessionId: state.turn.sessionId,
          startedAt: state.turn.startedAt ?? Date.now(),
          lastDeltaAt: null,
          lastEventType: "started",
        },
      };
    case "DELTA": {
      // Drop out-of-order deltas for a lower turn.
      if (
        action.turn !== null &&
        state.turn.turnSeq !== null &&
        action.turn < state.turn.turnSeq
      ) {
        return state;
      }
      return {
        ...state,
        turn: {
          ...state.turn,
          state: "running",
          text: state.turn.text + action.text,
          lastDeltaAt: Date.now(),
          lastEventType: "delta",
        },
      };
    }
    case "COMPLETED": {
      const finalText = action.text || state.turn.text;
      if (!finalText) {
        // The session died with no assistant text — surface the reason
        // ("The agent stopped: <reason>" + Retry), never an empty bubble.
        return {
          ...state,
          sending: false,
          error: action.error ?? "the session ended without a reply",
          turn: { ...IDLE_TURN, state: "completed", lastEventType: "completed" },
        };
      }
      const agentMsg: ChatMessage = {
        id: `sse_${action.sessionId ?? "turn"}_${action.turn ?? Date.now()}`,
        seq: (state.messages[state.messages.length - 1]?.seq ?? 0) + 1,
        role: "agent",
        content: finalText,
        model: action.model ?? state.turn.model,
        created_at: new Date().toISOString(),
        meta: {
          ...(action.sessionId ? { session_id: action.sessionId } : {}),
          ...(action.turn !== null ? { turn: action.turn } : {}),
          ...(action.timedOut ? { timed_out: true } : {}),
        },
      };
      // Dedupe on meta.session_id + turn: the poll fallback may have already
      // pulled the persisted row in.
      const dupe = state.messages.some(
        (m) =>
          m.role === "agent" &&
          action.sessionId !== null &&
          m.meta?.session_id === action.sessionId &&
          (action.turn === null || m.meta?.turn === action.turn),
      );
      return {
        ...state,
        sending: false,
        messages: dupe ? state.messages : dedupeMessages([...state.messages, agentMsg]),
        turn: {
          ...IDLE_TURN,
          state: action.timedOut ? "timed_out" : "completed",
          lastEventType: "completed",
          fanoutTaskIds: state.turn.fanoutTaskIds,
        },
      };
    }
    case "STALLED":
      if (state.turn.state !== "running" && state.turn.state !== "queued") return state;
      return { ...state, turn: { ...state.turn, state: "stalled" } };
    case "POLL_MESSAGES": {
      // Fallback poll (bridge dead): a terminal agent message settles the turn.
      const lastUser = [...state.messages].reverse().find((m) => m.role === "user");
      const settled = action.messages.some(
        (m) => m.role === "agent" && (!lastUser || m.seq > lastUser.seq),
      );
      return {
        ...state,
        messages: mergePolledMessages(state.messages, action.messages),
        turn: settled
          ? { ...IDLE_TURN, state: "completed", lastEventType: "poll" }
          : { ...state.turn, lastEventType: "poll" },
      };
    }
    case "DISMISS_SUGGESTION":
      return { ...state, suggestionDismissed: true };
    case "RESET":
      return {
        chat: null,
        messages: [],
        loading: false,
        error: null,
        sending: false,
        turn: IDLE_TURN,
        suggestionDismissed: false,
      };
  }
}

const INITIAL_STATE: ThreadState = {
  chat: null,
  messages: [],
  loading: false,
  error: null,
  sending: false,
  turn: IDLE_TURN,
  suggestionDismissed: false,
};

const STALL_AFTER_MS = 6_000;
const POLL_INTERVAL_MS = 3_000;
const POLL_TIMEOUT_MS = 15 * 60_000;

export interface SendOptions {
  model?: string | null;
  orchMode?: OrchMode | null;
  count?: number;
  attachments?: Attachment[];
  meta?: ChatComposerMeta;
}

export type ThreadActions = {
  refresh: () => Promise<void>;
  send: (content: string, options?: SendOptions) => Promise<void>;
  patch: (patch: ChatPatchRequest) => Promise<Chat | null>;
  spawn: (goal: string, count?: number) => Promise<SpawnResponse>;
  promote: (projectId: string | null, newProjectTitle?: string) => Promise<PromoteResponse>;
  uploadFiles: (files: File[]) => Promise<Array<{ name: string; path: string }>>;
  dismissSuggestion: () => void;
  applySuggestion: (projectId: string | null) => Promise<void>;
  startPlan: (goal?: string | null, speed?: RoutingSpeed) => Promise<void>;
  confirmPlan: (projectOverride?: string, speed?: RoutingSpeed) => Promise<void>;
  discardPlan: () => void;
  planJob: PlanJobStatus | null;
  planBusy: boolean;
  planError: string | null;
};

export function useChatThread(
  chatId: string | null,
): ThreadState & { source: DataSource } & ThreadActions {
  const [state, dispatch] = useReducer(reducer, INITIAL_STATE);
  const [source, setSource] = useState<DataSource>("live");
  const [planJob, setPlanJob] = useState<PlanJobStatus | null>(null);
  const [planBusy, setPlanBusy] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const stateRef = useRef(state);
  stateRef.current = state;

  // ── Load chat + messages (synchronous from cache, then revalidate) ──

  const refresh = useCallback(async () => {
    if (!chatId) return;
    const cached = threadCache.get(chatId);
    if (cached && Date.now() - cached.fetchedAt < CACHE_FRESH_MS) {
      try {
        const chat = await chatApi.getChat(chatId);
        dispatch({ type: "LOADED", chat, messages: cached.messages });
      } catch (reason) {
        dispatch({ type: "ERROR", error: errText(reason, "Could not load the chat") });
      }
      // revalidate in background
      void chatApi
        .listMessages(chatId)
        .then((messages) => {
          cacheThread(chatId, messages);
          dispatch({ type: "POLL_MESSAGES", messages });
        })
        .catch(() => undefined);
      return;
    }
    dispatch({ type: "LOADING" });
    try {
      const [chat, messages] = await Promise.all([
        chatApi.getChat(chatId),
        chatApi.listMessages(chatId),
      ]);
      cacheThread(chatId, messages);
      setSource(USE_CHAT_FIXTURES ? "fixtures" : "live");
      dispatch({ type: "LOADED", chat, messages });
    } catch (reason) {
      dispatch({ type: "ERROR", error: errText(reason, "Could not load the chat") });
      setSource("fixtures");
    }
  }, [chatId]);

  useEffect(() => {
    dispatch({ type: "RESET" });
    setPlanJob(null);
    setPlanError(null);
    if (chatId) void refresh();
  }, [chatId, refresh]);

  // ── Chat DTO revalidation (classify chip + server-set title) ──
  // The first-message hooks write title + meta.project_suggestion SERVER-side
  // without an SSE event, so the DTO goes stale in-session: the suggestion
  // bar (§2.4) and the renamed title would never appear until a reload.
  // Refetch on turn completion and once shortly after each send (classify
  // takes seconds); failures stay silent — the bar simply stays hidden (§2.8).

  const refetchChat = useCallback(async () => {
    if (!chatId) return;
    try {
      const chat = await chatApi.getChat(chatId);
      dispatch({ type: "CHAT_UPDATED", chat });
    } catch {
      /* silent — a stale DTO is a missed suggestion, never an error state */
    }
  }, [chatId]);

  const dtoRefreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (dtoRefreshTimer.current) clearTimeout(dtoRefreshTimer.current);
    },
    [chatId],
  );

  const scheduleDtoRefresh = useCallback(() => {
    if (dtoRefreshTimer.current) clearTimeout(dtoRefreshTimer.current);
    dtoRefreshTimer.current = setTimeout(() => {
      dtoRefreshTimer.current = null;
      void refetchChat();
    }, 8_000);
  }, [refetchChat]);

  // ── SSE streaming (started stays server-side; delta/completed are bridge-only) ──

  const { lastEvent } = useEventChannel([
    "chat.turn.started" as string,
    "chat.turn.delta" as string,
    "chat.turn.completed" as string,
  ]);

  useEffect(() => {
    if (!lastEvent || !chatId) return;
    const evt = lastEvent.payload as unknown as ChatTurnEvent | undefined;
    if (!evt) return;
    const type = lastEvent.type as string;
    const evtChatId =
      (typeof evt.chat_id === "string" && evt.chat_id) ||
      (lastEvent.target_id === chatId ? chatId : "");
    if (evtChatId !== chatId) return;

    if (type === "chat.turn.started") {
      dispatch({
        type: "TURN_STARTED",
        model: (typeof evt.model === "string" && evt.model) || null,
        turn: typeof evt.turn === "number" ? evt.turn : null,
      });
    } else if (type === "chat.turn.delta") {
      dispatch({
        type: "DELTA",
        text: typeof evt.text === "string" ? evt.text : "",
        turn: typeof evt.turn === "number" ? evt.turn : null,
      });
    } else if (type === "chat.turn.completed") {
      dispatch({
        type: "COMPLETED",
        text: typeof evt.text === "string" ? evt.text : "",
        model: (typeof evt.model === "string" && evt.model) || null,
        turn: typeof evt.turn === "number" ? evt.turn : null,
        sessionId:
          (typeof (evt as { session_id?: unknown }).session_id === "string"
            ? (evt as { session_id?: string }).session_id
            : null) ?? stateRef.current.turn.sessionId,
        timedOut: Boolean(evt.timed_out),
        error:
          typeof (evt as { error?: unknown }).error === "string"
            ? ((evt as { error?: string }).error ?? null)
            : null,
      });
      // The turn's first-message hooks (title, classify) have landed by now.
      void refetchChat();
    }
  }, [lastEvent, chatId, refetchChat]);

  // ── 6s stall + 3s poll fallback (§P0-1 client fallback, non-negotiable) ──

  const turnActive = state.turn.state === "running" || state.turn.state === "queued" || state.turn.state === "stalled";
  const noDeltaYet = state.turn.state !== "idle" && state.turn.lastDeltaAt === null;

  useEffect(() => {
    if (!chatId || !turnActive) return;
    const stallTimer = setTimeout(() => {
      if (stateRef.current.turn.lastDeltaAt === null) {
        dispatch({ type: "STALLED" });
      }
    }, STALL_AFTER_MS);
    return () => clearTimeout(stallTimer);
  }, [chatId, turnActive, state.turn.turnSeq, noDeltaYet]);

  useEffect(() => {
    if (!chatId || state.turn.state !== "stalled") return;
    const startedAt = state.turn.startedAt ?? Date.now();
    const interval = setInterval(() => {
      if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
        clearInterval(interval);
        return;
      }
      void chatApi
        .listMessages(chatId)
        .then((messages) => {
          cacheThread(chatId, messages);
          dispatch({ type: "POLL_MESSAGES", messages });
        })
        .catch(() => undefined);
    }, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [chatId, state.turn.state, state.turn.startedAt]);

  // ── Actions ──

  const send = useCallback(
    async (content: string, options?: SendOptions) => {
      const current = stateRef.current;
      if (!chatId || !content.trim()) return;
      const optimistic: ChatMessage = {
        id: `optimistic_${Date.now()}`,
        seq: (current.messages[current.messages.length - 1]?.seq ?? 0) + 1,
        role: "user",
        content: content.trim(),
        model: options?.model ?? null,
        created_at: new Date().toISOString(),
        meta: {
          ...(options?.meta ?? {}),
          ...(options?.attachments?.length ? { attachments: options.attachments } : {}),
        },
      };
      dispatch({ type: "SENT", optimistic });
      try {
        const result = await chatApi.sendMessage(chatId, {
          content: content.trim(),
          model: options?.model ?? undefined,
          orch_mode: options?.orchMode ?? undefined,
          count: options?.count,
          meta: {
            ...(options?.meta ?? {}),
            ...(options?.attachments?.length ? { attachments: options.attachments } : {}),
          },
        });
        cacheThread(
          chatId,
          stateRef.current.messages.map((m) =>
            m.id === optimistic.id ? result.message : m,
          ),
        );
        dispatch({
          type: "SEND_DONE",
          optimisticId: optimistic.id,
          message: result.message,
          sessionId: result.dispatch?.session_id ?? null,
          fanoutTaskIds: result.task_ids ?? null,
        });
        // Title is set synchronously during the POST (first message); the
        // project classify lands seconds later — catch both (see above).
        void refetchChat();
        scheduleDtoRefresh();
      } catch (reason) {
        dispatch({
          type: "SEND_FAILED",
          optimisticId: optimistic.id,
          error: errText(reason, "Could not send message"),
        });
        throw reason;
      }
    },
    [chatId, refetchChat, scheduleDtoRefresh],
  );

  const patch = useCallback(
    async (body: ChatPatchRequest): Promise<Chat | null> => {
      if (!chatId) return null;
      const chat = await chatApi.updateChat(chatId, body);
      dispatch({ type: "CHAT_UPDATED", chat });
      return chat;
    },
    [chatId],
  );

  const spawn = useCallback(
    async (goal: string, count?: number): Promise<SpawnResponse> => {
      if (!chatId) return { task_ids: [] };
      return chatApi.spawnChat(chatId, goal, count);
    },
    [chatId],
  );

  const promote = useCallback(
    async (projectId: string | null, newProjectTitle?: string): Promise<PromoteResponse> => {
      if (!chatId) return { project_id: "", task_ids: [] };
      const result = await chatApi.promoteChat(chatId, projectId, newProjectTitle);
      await refresh();
      return result;
    },
    [chatId, refresh],
  );

  const uploadFiles = useCallback(
    async (files: File[]): Promise<Array<{ name: string; path: string }>> => {
      const chat = stateRef.current.chat;
      if (!chatId || !files.length) return [];
      const taskId = chat?.board_task_id;
      if (!taskId) {
        throw new Error("No companion task for file uploads");
      }
      const result = await chatApi.uploadChatFile(taskId, files);
      return result.saved.map((s) => ({ name: s.name, path: s.path }));
    },
    [chatId],
  );

  const dismissSuggestion = useCallback(() => {
    dispatch({ type: "DISMISS_SUGGESTION" });
  }, []);

  const applySuggestion = useCallback(
    async (projectId: string | null) => {
      if (!chatId || !projectId) return;
      await patch({ project_id: projectId });
      dispatch({ type: "DISMISS_SUGGESTION" });
    },
    [chatId, patch],
  );

  // ── Plan mode (§2.6): seed → poll → PlanCard → confirm ──

  const startPlan = useCallback(
    async (goal?: string | null, speed?: RoutingSpeed) => {
      if (!chatId) return;
      setPlanBusy(true);
      setPlanError(null);
      try {
        const seed = await chatApi.seedPlan(chatId, goal ?? null, speed ?? null);
        // Poll until ready/error; PlanCard renders from planJob.
        const deadline = Date.now() + 5 * 60_000;
        for (;;) {
          const job = await chatApi.fetchPlanJob(seed.job_id);
          setPlanJob(job);
          if (job.status !== "running" || Date.now() > deadline) break;
          await new Promise((resolve) => setTimeout(resolve, 2000));
        }
      } catch (reason) {
        setPlanError(errText(reason, "Planning failed"));
      } finally {
        setPlanBusy(false);
      }
    },
    [chatId],
  );

  // Re-attach to a running job after reload (meta.plan_job_id, §3.6).
  useEffect(() => {
    const jobId = state.chat?.meta?.plan_job_id;
    if (!chatId || typeof jobId !== "string" || !jobId || planJob) return;
    void chatApi
      .fetchPlanJob(jobId)
      .then((job) => {
        if (job.status === "running" || job.status === "ready") setPlanJob(job);
      })
      .catch(() => undefined);
    // only on chat load
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatId, state.chat?.id]);

  const confirmPlan = useCallback(
    async (projectOverride?: string, speed?: RoutingSpeed) => {
      if (!planJob) return;
      setPlanBusy(true);
      setPlanError(null);
      try {
        await chatApi.confirmPlanJob(planJob.job_id, projectOverride ?? "auto", speed ?? null);
        setPlanJob(null);
        await patch({ meta: { plan_job_id: "" } });
        await refresh();
      } catch (reason) {
        setPlanError(errText(reason, "Could not confirm the plan"));
      } finally {
        setPlanBusy(false);
      }
    },
    [planJob, patch, refresh],
  );

  const discardPlan = useCallback(() => {
    setPlanJob(null);
    setPlanError(null);
    if (chatId) void patch({ meta: { plan_job_id: "" } });
  }, [chatId, patch]);

  return {
    ...state,
    source,
    refresh,
    send,
    patch,
    spawn,
    promote,
    uploadFiles,
    dismissSuggestion,
    applySuggestion,
    startPlan,
    confirmPlan,
    discardPlan,
    planJob,
    planBusy,
    planError,
  };
}

// ── useSkillsForMention ───────────────────────────────────────

export function useSkillsForMention() {
  const [skills, setSkills] = useState<SkillTreeEntry[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const result = await chatApi.fetchSkillsTree();
      setSkills(result);
    } catch {
      setSkills([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { skills, loading, refresh };
}

// ── Shared chat event re-fetch ────────────────────────────────

/**
 * Listen for events that change the chat list's derived fields
 * (message_count, last_message_at, title) and call refresh when one lands.
 *
 * The subscription names the types explicitly: ``board.updated`` (dispatches
 * move the companion card) and the ``chat.turn.*`` family (the bridge's
 * reply write-back bumps the counters). Subscribing to "board.updated" alone
 * filtered every chat frame out — ``board.updated`` always carries
 * ``target_type="board_task"``, so a ``target_type === "chat"`` check on it
 * was dead code.
 */
export function useChatRefreshSignal(refresh: () => void) {
  const { lastEvent } = useEventChannel([
    "board.updated" as string,
    "chat.turn.completed" as string,
  ]);

  useEffect(() => {
    if (
      lastEvent &&
      (lastEvent.type === "board.updated" ||
        lastEvent.type === "chat.turn.completed" ||
        lastEvent.target_type === "chat")
    ) {
      void Promise.resolve(refresh());
    }
  }, [lastEvent, refresh]);
}
