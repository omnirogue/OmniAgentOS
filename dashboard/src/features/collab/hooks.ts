"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { collabApi, liveBoardConditional } from "./client";
import { EVENTS_BASE } from "@/lib/contracts";
import { eventCursorKey, readStoredLastId, writeStoredLastId } from "@/lib/useEvents";
import { startVisibilityPoll } from "@/lib/pollWhenVisible";
import { USE_FIXTURES } from "./fixtures";
import type { Agent, ArchiveTasksResult, Channel, LiveBoardTask, Message, MessageKind } from "./types";

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

/**
 * Build the collab EventSource URL against product-scoped EVENTS_BASE (H-18 / L-03).
 *
 * Always carries BOTH halves of the client contract:
 *  - `types=` — only the events this connection handles, filtered server-side.
 *  - `after_id=` — the cursor this filter last saw (sessionStorage, per filter).
 *    Without it, every reconnect — and a phone wakes an EventSource up a lot —
 *    replays the server's whole 500-row window before the first live frame.
 */
export function collabEventsUrl(types: string[], afterId: number): string {
  const params = new URLSearchParams();
  params.set("types", types.join(","));
  params.set("after_id", String(afterId));
  return `${EVENTS_BASE}/api/events?${params.toString()}`;
}

/** Re-fetch on the collaboration SSE events while retaining polling-free empty states. */
function useCollabUpdates(
  types: string[],
  refresh: () => Promise<void>,
  onStreamError?: (message: string) => void,
  onStreamOpen?: () => void,
) {
  const typesKey = types.join(",");
  useEffect(() => {
    if (USE_FIXTURES) return;
    const cursorKey = eventCursorKey(typesKey);
    const source = new EventSource(
      collabEventsUrl(typesKey.split(","), readStoredLastId(cursorKey)),
    );
    let debounceTimer: ReturnType<typeof setTimeout> | null = null;
    const update = (event: MessageEvent) => {
      // The frame's SSE `id:` IS the events-table id, so advancing the cursor
      // needs no payload parsing — this hook treats events purely as "something
      // changed, refetch" and never reads their bodies.
      const seen = Number(event.lastEventId);
      if (Number.isFinite(seen) && seen > readStoredLastId(cursorKey)) {
        writeStoredLastId(seen, cursorKey);
      }
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        debounceTimer = null;
        void refresh();
      }, 2000);
    };
    typesKey.split(",").forEach((type) => source.addEventListener(type, update));
    source.onmessage = update;
    source.onopen = () => {
      onStreamOpen?.();
    };
    // Surface initial/reconnect failures so operators do not see a silent dead stream.
    source.onerror = () => {
      onStreamError?.("Live collaboration events failed to connect.");
    };
    return () => {
      source.close();
      if (debounceTimer) clearTimeout(debounceTimer);
    };
  }, [onStreamError, onStreamOpen, refresh, typesKey]);
}

export function useAgents() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => { setLoading(true); try { setAgents(await collabApi.agents()); setError(null); } catch (reason) { setError(errorMessage(reason, "Failed to load agents")); } finally { setLoading(false); } }, []);
  const onStreamError = useCallback((message: string) => setError(message), []);
  useEffect(() => { void refresh(); }, [refresh]);
  useCollabUpdates(["agent.updated"], refresh, onStreamError);
  return { agents, loading, error, refresh };
}

// useBoardTasks() is GONE with the endpoint it read: it was the only caller of
// `GET /api/collab/board` (removed — a duplicate, unauthenticated listing of the
// same table as `GET /api/board`), and nothing in the dashboard mounted it.
// The live board hook below — reconciled cards, archive support, SSE + poll
// fallback — is the board read.

/**
 * The LIVE kanban feed: reads GET /api/board (each card reconciled from its linked
 * run) and refreshes on the collab + run/task SSE events, with a 30s safety-net
 * poll so a runner claiming/completing a task is still reflected if the SSE
 * stream is down — the SSE push, not the interval, is the real-time driver.
 *
 * `includeArchived` (default false) switches the query to only archived cards
 * (?archived=1) instead of merging them into the live board.
 * `projectId` (optional) narrows the server query to a single project —
 * mirrors the ?project=<id> deep-link on the board page.
 *
 * CONDITIONAL: the last response's `ETag` is remembered and replayed as
 * `If-None-Match`. A `304` means "nothing this board reads has moved" and is
 * treated as a successful refresh that KEEPS the current cards — it must never
 * be read as an empty board, and it must still clear the error/reconnecting
 * state, because a 304 is a healthy answer from a healthy server.
 */
export function useLiveBoard(includeArchived = false, projectId?: string) {
  const [tasks, setTasks] = useState<LiveBoardTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);
  const [connected, setConnected] = useState(false);
  const [needsResponseIds, setNeedsResponseIds] = useState<ReadonlySet<string>>(() => new Set());
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const retryAttempt = useRef(0);
  const inFlight = useRef(false);
  const pendingRerun = useRef(false);
  const refreshRef = useRef<(() => Promise<void>) | null>(null);
  const etag = useRef<string | null>(null);
  // The tag validates ONE query. Changing the query (archived / project scope)
  // makes the held tag meaningless — replaying it would let the server answer
  // 304 against a board the user is no longer looking at.
  useEffect(() => {
    etag.current = null;
  }, [includeArchived, projectId]);
  const refresh = useCallback(async () => {
    if (inFlight.current) {
      pendingRerun.current = true;
      return;
    }
    inFlight.current = true;
    try {
      const result = await liveBoardConditional({
        archived: includeArchived,
        ...(projectId ? { project_id: projectId } : {}),
        etag: etag.current,
      });
      etag.current = result.etag;
      // 304 → result.tasks is null → keep the cards already on screen.
      if (result.tasks !== null) {
        setTasks(result.tasks);
        // The Needs You band is SERVER-ranked (GET /api/board/needs-response,
        // the same strict predicate the API owns) and is refreshed in lockstep
        // with the cards. A 304 board cannot have a different band — the band
        // is derived from the very rows the ETag covers — so this rides the
        // same conditional saving. Failure is silent: the kanban falls back to
        // the client predicate, which is also the SSE-interim answer between a
        // card changing and this list catching up.
        if (!includeArchived) {
          void collabApi
            .needsResponse()
            .then((band) => setNeedsResponseIds(new Set(band.items.map((item) => item.id))))
            .catch(() => undefined);
        }
      }
      setHasLoaded(true);
      setError(null);
      setReconnecting(false);
      retryAttempt.current = 0;
      if (retryTimer.current) clearTimeout(retryTimer.current);
      retryTimer.current = null;
    } catch (reason) {
      const message = errorMessage(reason, "Failed to load the board");
      setError(message);
      setReconnecting(true);
      if (!retryTimer.current) {
        const delay = Math.min(2000 * (2 ** retryAttempt.current), 30000);
        retryAttempt.current += 1;
        retryTimer.current = setTimeout(() => {
          retryTimer.current = null;
          void refreshRef.current?.();
        }, delay);
      }
    } finally {
      setLoading(false);
      inFlight.current = false;
      if (pendingRerun.current) {
        pendingRerun.current = false;
        void refreshRef.current?.();
      }
    }
  }, [includeArchived, projectId]);
  refreshRef.current = refresh;
  const archiveTask = useCallback(async (taskId: string) => {
    await collabApi.archiveTask(taskId);
    await refresh();
  }, [refresh]);
  const archiveTasks = useCallback(async (taskIds: string[]): Promise<ArchiveTasksResult> => {
    const result = await collabApi.archiveTasks(taskIds);
    await refresh();
    return result;
  }, [refresh]);
  const onStreamError = useCallback((message: string) => {
    setError(message);
    setReconnecting(true);
    setConnected(false);
  }, []);
  const onStreamOpen = useCallback(() => setConnected(true), []);
  useEffect(() => { void refresh(); }, [refresh]);
  useCollabUpdates(
    ["board.updated", "board.claimed", "run.updated", "task.updated", "session.updated"],
    refresh,
    onStreamError,
    onStreamOpen,
  );
  useEffect(() => {
    if (USE_FIXTURES) return;
    return startVisibilityPoll(() => void refresh(), 30000);
  }, [refresh]);
  useEffect(() => () => {
    if (retryTimer.current) clearTimeout(retryTimer.current);
  }, []);
  return {
    tasks,
    loading,
    error,
    hasLoaded,
    reconnecting,
    connected,
    /** Card ids the SERVER put in the Needs Response band. Empty when the
     * endpoint is unavailable — never a reason to empty the Needs You column. */
    needsResponseIds,
    refresh,
    archiveTask,
    archiveTasks,
  };
}

export function useChannels() {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => { setLoading(true); try { setChannels(await collabApi.channels()); setError(null); } catch (reason) { setError(errorMessage(reason, "Failed to load channels")); } finally { setLoading(false); } }, []);
  const onStreamError = useCallback((message: string) => setError(message), []);
  useEffect(() => { void refresh(); }, [refresh]);
  useCollabUpdates(["channel.updated"], refresh, onStreamError);
  return { channels, loading, error, refresh };
}

export function useMessages(channelId: string | null) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const currentChannelRef = useRef(channelId);
  currentChannelRef.current = channelId;
  const refresh = useCallback(async () => {
    if (!channelId) { setMessages([]); setLoading(false); return; }
    setLoading(true);
    try { setMessages(await collabApi.messages(channelId)); setError(null); } catch (reason) { setError(errorMessage(reason, "Failed to load messages")); } finally { setLoading(false); }
  }, [channelId]);
  const postMessage = useCallback(async (body: string, kind?: MessageKind, ref?: string) => {
    if (!currentChannelRef.current) throw new Error("Choose a channel before sending a message");
    const message = await collabApi.postMessage(currentChannelRef.current, body, kind, ref);
    setMessages((current) => [...current, message]);
    return message;
  }, []);
  const searchMessages = useCallback((q: string) => q.trim() ? collabApi.searchMessages(q.trim()) : Promise.resolve<Message[]>([]), []);
  const onStreamError = useCallback((message: string) => setError(message), []);
  useEffect(() => { void refresh(); }, [refresh]);
  useCollabUpdates(["message.posted"], refresh, onStreamError);
  return { messages, loading, error, refresh, postMessage, searchMessages };
}
