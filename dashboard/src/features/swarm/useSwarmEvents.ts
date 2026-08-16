"use client";

/**
 * SSE hook for the swarm activity stream (C3).
 *
 * A verbatim structural CLONE of `lib/useReliabilityEvents.ts` (which itself
 * mirrors the FROZEN `lib/useEvents.ts` without touching it): EventSource on
 * `GET /api/events`, an `after_id` cursor persisted in sessionStorage, a 1.5s
 * reconnect, and `resync` handling. It does NOT edit the frozen
 * `useEvents`/`EVENT_TYPES` contract.
 *
 * Swarm activity is a SINGLE event type on the shared `events` table
 * (`type="swarm.event"`, `actor="swarm"`, the change named by `action` —
 * `contracts/swarm-api.md`), exactly like the reliability V2 events reuse it.
 * So this filters to `types=swarm.event` (one `event: swarm.event` frame per
 * row) and the caller routes on `row.action`.
 *
 * Frames are DEBOUNCED REFRESH HINTS ONLY (see `useSwarmEventRouter` in
 * `hooks.ts`): REST stays the source of truth and every panel keeps its own
 * poll fallback. This hook only surfaces the frames.
 */

import { useEffect, useRef, useState } from "react";
import { EVENTS_BASE } from "@/lib/contracts";

/** The single swarm SSE frame type (a plain string on the shared events table,
 * not part of the frozen `EVENT_TYPES`). */
export const SWARM_EVENT_TYPE = "swarm.event";

/** The `swarm.event` action vocabulary (`omniagentos.swarm.contracts
 * .SWARM_EVENT_ACTIONS` / `contracts/swarm-api.md`). Used by the refresh router
 * to decide which REST reads a hint should refetch; an unrecognized action
 * refreshes everything, so this list can lag the backend safely. */
export const SWARM_EVENT_ACTIONS = [
  "plan_created",
  "run_started",
  "slot_opened",
  "task_assigned",
  "task_completed",
  "review_confirmed",
  "review_denied",
  "provider_switched",
  "rate_limit",
  "rate_limit_stall",
  "task_split",
  "resize",
  "task_blocked",
  "approval_parked",
  "merge_started",
  "run_completed",
  "run_failed",
] as const;

/** Generic SSE row shape (mirrors `EventRow`/`ReliabilitySseRow` without
 * depending on the frozen `EventType`). */
export interface SwarmSseRow {
  id: number;
  type: string;
  ts: string;
  actor: string;
  action: string;
  target_type: string;
  target_id: string;
  payload: Record<string, unknown>;
  trace_id: string;
}

export interface UseSwarmEventsResult {
  events: SwarmSseRow[];
  lastEvent: SwarmSseRow | null;
  connected: boolean;
}

const LAST_ID_KEY = "omniagentos:swarm:lastEventId";
const RECONNECT_MS = 1500;

function readStoredLastId(): number {
  if (typeof sessionStorage === "undefined") return 0;
  try {
    const raw = sessionStorage.getItem(LAST_ID_KEY);
    if (!raw) return 0;
    const n = Number(raw);
    return Number.isFinite(n) && n >= 0 ? n : 0;
  } catch {
    return 0;
  }
}

function writeStoredLastId(id: number): void {
  if (typeof sessionStorage === "undefined") return;
  try {
    sessionStorage.setItem(LAST_ID_KEY, String(id));
  } catch {
    // ignore quota / private-mode failures
  }
}

function parseEventRow(data: string): SwarmSseRow | null {
  try {
    const row = JSON.parse(data) as SwarmSseRow;
    if (row == null || typeof row !== "object") return null;
    return row;
  } catch {
    return null;
  }
}

function makeResyncRow(latestId: number): SwarmSseRow {
  return {
    id: latestId,
    type: "resync",
    ts: new Date().toISOString(),
    actor: "system",
    action: "resync",
    target_type: "",
    target_id: "",
    payload: { latest_id: latestId },
    trace_id: "",
  };
}

function appendCapped(
  prev: SwarmSseRow[],
  row: SwarmSseRow,
  maxBuffer: number,
): SwarmSseRow[] {
  if (prev.some((e) => e.id === row.id && e.type === row.type)) {
    return prev;
  }
  const next = [...prev, row];
  if (next.length <= maxBuffer) return next;
  return next.slice(next.length - maxBuffer);
}

/**
 * Subscribe to the `swarm.event` SSE frame stream.
 * @param maxBuffer cap on retained rows
 */
export function useSwarmEvents(maxBuffer = 200): UseSwarmEventsResult {
  const [events, setEvents] = useState<SwarmSseRow[]>([]);
  const [lastEvent, setLastEvent] = useState<SwarmSseRow | null>(null);
  const [connected, setConnected] = useState(false);
  const lastIdRef = useRef<number>(0);
  const maxBufferRef = useRef(maxBuffer);
  maxBufferRef.current = maxBuffer;

  useEffect(() => {
    lastIdRef.current = readStoredLastId();

    let closed = false;
    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const handleRow = (row: SwarmSseRow) => {
      // Only surface swarm frames (ignore any stray type on the shared stream).
      if (row.type !== "resync" && row.type !== SWARM_EVENT_TYPE) return;
      if (typeof row.id === "number" && row.id > lastIdRef.current) {
        lastIdRef.current = row.id;
        writeStoredLastId(row.id);
      }
      setEvents((prev) => appendCapped(prev, row, maxBufferRef.current));
      setLastEvent(row);
    };

    const handleResync = (data: string) => {
      let latestId = 0;
      try {
        const payload = JSON.parse(data) as { latest_id?: number };
        latestId = typeof payload.latest_id === "number" ? payload.latest_id : 0;
      } catch {
        latestId = 0;
      }
      lastIdRef.current = latestId;
      writeStoredLastId(latestId);
      const synthetic = makeResyncRow(latestId);
      setEvents([]);
      setLastEvent(synthetic);
    };

    const connect = () => {
      if (closed) return;
      if (typeof EventSource === "undefined") return;

      const params = new URLSearchParams();
      params.set("after_id", String(lastIdRef.current));
      params.set("types", SWARM_EVENT_TYPE);

      const url = `${EVENTS_BASE}/api/events?${params.toString()}`;
      es = new EventSource(url);

      es.onopen = () => {
        if (!closed) setConnected(true);
      };

      es.onerror = () => {
        if (closed) return;
        setConnected(false);
        // Re-open with the latest after_id so the frozen query-param cursor
        // (not a stale URL) is used after reconnect.
        es?.close();
        es = null;
        if (reconnectTimer != null) clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, RECONNECT_MS);
      };

      // Named SSE frames use event:<type>; onmessage only fires for "message".
      es.addEventListener(SWARM_EVENT_TYPE, (ev: MessageEvent) => {
        if (closed) return;
        const row = parseEventRow(ev.data);
        if (row) handleRow(row);
      });

      es.addEventListener("resync", (ev: MessageEvent) => {
        if (closed) return;
        handleResync(ev.data);
      });

      // Fallback if the server ever emits an untyped frame.
      es.onmessage = (ev: MessageEvent) => {
        if (closed) return;
        const row = parseEventRow(ev.data);
        if (row) handleRow(row);
      };
    };

    connect();

    return () => {
      closed = true;
      if (reconnectTimer != null) clearTimeout(reconnectTimer);
      es?.close();
      setConnected(false);
    };
  }, []);

  return { events, lastEvent, connected };
}
