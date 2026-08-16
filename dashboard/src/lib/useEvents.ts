"use client";

/**
 * SSE hook. SIGNATURE FROZEN (Wave 0) — p08 implements the body (EventSource on
 * GET /api/events per contracts/events.md, reconnect with last seen id). p09 codes
 * against this signature concurrently.
 */

import { useEffect, useRef, useState } from "react";
import { EVENTS_BASE, EVENT_TYPES, type EventRow, type EventType } from "./contracts";

export interface UseEventsResult {
  events: EventRow[];
  lastEvent: EventRow | null;
  connected: boolean;
  /** True after an EventSource onerror until the next successful open. Additive for L-09 stream failure surface. */
  error: boolean;
}

const LAST_ID_KEY = "omniagentos:lastEventId";
const RECONNECT_MS = 1500;

/**
 * Per-filter SSE cursor storage.
 *
 * Every EventSource here reconnects with `after_id`, so the server replays from
 * where that connection left off instead of handing a fresh phone the tail of
 * the whole ring buffer (up to 500 rows) on every reconnect.
 *
 * The cursor is keyed by the connection's `types` filter and NEVER shared
 * across different filters: a cursor advanced by a board-events stream would
 * make a revenue-events stream skip every revenue event with a lower id — rows
 * it never actually received. One filter, one cursor.
 */
export function eventCursorKey(types?: string): string {
  return types ? `${LAST_ID_KEY}:${types}` : LAST_ID_KEY;
}

export function readStoredLastId(key: string = LAST_ID_KEY): number {
  if (typeof sessionStorage === "undefined") return 0;
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return 0;
    const n = Number(raw);
    return Number.isFinite(n) && n >= 0 ? n : 0;
  } catch {
    return 0;
  }
}

export function writeStoredLastId(id: number, key: string = LAST_ID_KEY): void {
  if (typeof sessionStorage === "undefined") return;
  try {
    sessionStorage.setItem(key, String(id));
  } catch {
    // ignore quota / private-mode failures
  }
}

function parseEventRow(data: string): EventRow | null {
  try {
    const row = JSON.parse(data) as EventRow;
    if (row == null || typeof row !== "object") return null;
    return row;
  } catch {
    return null;
  }
}

function makeResyncRow(latestId: number): EventRow {
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

function appendCapped(prev: EventRow[], row: EventRow, maxBuffer: number): EventRow[] {
  // Prefer newest; drop oldest when over capacity.
  if (prev.some((e) => e.id === row.id && e.type === row.type)) {
    return prev;
  }
  const next = [...prev, row];
  if (next.length <= maxBuffer) return next;
  return next.slice(next.length - maxBuffer);
}

export function useEvents(types?: EventType[], maxBuffer = 200): UseEventsResult {
  const [events, setEvents] = useState<EventRow[]>([]);
  const [lastEvent, setLastEvent] = useState<EventRow | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState(false);
  const lastIdRef = useRef<number>(0);
  const maxBufferRef = useRef(maxBuffer);
  maxBufferRef.current = maxBuffer;

  // Stable key so parent re-renders with a fresh array literal don't thrash the socket.
  const typesKey = types?.length ? types.join(",") : "";

  useEffect(() => {
    // A new filter is a new feed. The buffer holds rows the PREVIOUS filter
    // matched, and nothing else will ever remove them — switching from board
    // events to revenue events would leave board rows sitting in a revenue
    // list until enough revenue events arrived to push them out, one at a
    // time. Cleared here, so the buffer only ever contains rows this
    // connection's filter actually selected.
    setEvents([]);

    // The types this connection actually handles — always sent to the server as
    // `types=` so it filters at the source instead of shipping every event in
    // the ring buffer for the client to throw away.
    const listenTypes: string[] = typesKey ? typesKey.split(",") : [...EVENT_TYPES];
    const filter = listenTypes.join(",");
    const cursorKey = eventCursorKey(filter);
    lastIdRef.current = readStoredLastId(cursorKey);

    let closed = false;
    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const handleRow = (row: EventRow) => {
      if (typeof row.id === "number" && row.id > lastIdRef.current) {
        lastIdRef.current = row.id;
        writeStoredLastId(row.id, cursorKey);
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
      writeStoredLastId(latestId, cursorKey);
      const synthetic = makeResyncRow(latestId);
      setEvents([]);
      setLastEvent(synthetic);
    };

    const connect = () => {
      if (closed) return;

      const params = new URLSearchParams();
      params.set("after_id", String(lastIdRef.current));
      // Always filtered server-side, even on the default (no argument) mount:
      // the hook only ever handles EVENT_TYPES, so asking for everything just
      // makes the server serialise frames this client drops.
      params.set("types", filter);

      const url = `${EVENTS_BASE}/api/events?${params.toString()}`;
      es = new EventSource(url);

      es.onopen = () => {
        if (!closed) {
          setConnected(true);
          setError(false);
        }
      };

      es.onerror = () => {
        if (closed) return;
        setConnected(false);
        setError(true);
        // Close and re-open with the latest after_id so the frozen query-param
        // cursor (not a stale URL) is used after reconnect.
        es?.close();
        es = null;
        if (reconnectTimer != null) clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, RECONNECT_MS);
      };

      // Named SSE frames use event:<type>; onmessage only fires for default "message".
      for (const type of listenTypes) {
        es.addEventListener(type, (ev: MessageEvent) => {
          if (closed) return;
          const row = parseEventRow(ev.data);
          if (row) handleRow(row);
        });
      }

      es.addEventListener("resync", (ev: MessageEvent) => {
        if (closed) return;
        handleResync(ev.data);
      });

      // Fallback if server emits untyped frames.
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
  }, [typesKey]);

  return { events, lastEvent, connected, error };
}
