"use client";

/**
 * ONE EventSource per tab (T1.4).
 *
 * Browsers cap HTTP/1.1 at 6 connections per origin. The dashboard used to open
 * one EventSource per hook (8 distinct call sites, 3 of them mounted on EVERY
 * page via `app/layout.tsx` + `design/AppShell.tsx`), so on a busy page the 7th
 * stream silently never connected — a live bug at a SINGLE tab, not just a
 * scale problem. This provider owns exactly one `EventSource` to
 * `GET /api/events` and fans every frame out to a subscriber registry;
 * `useEventChannel` (lib/useEventChannel.ts) is the consumer-side hook.
 *
 * It is a structural CLONE of the FROZEN `lib/useEvents.ts` — same
 * `omniagentos:lastEventId` sessionStorage cursor, same `after_id` query-param
 * reconnect (the SSE cursor this backend contract uses in place of a
 * `Last-Event-ID` header; the browser still replays that header on its own
 * reconnects, which is why we close-and-reopen with a fresh `after_id` instead
 * of letting EventSource auto-retry), same 1.5s reconnect delay, same `resync`
 * frame handling (clear buffers, adopt `latest_id`). `useEvents.ts` is FROZEN
 * (Wave 0) and is NOT edited or deleted — the same "clone without touching the
 * frozen file" precedent documented at `features/swarm/useSwarmEvents.ts:6`.
 *
 * The shared connection deliberately sends NO `types` query param, so the
 * server streams every kind and each subscriber filters client-side. Named SSE
 * frames arrive as `event:<type>`, so a listener must be registered per type;
 * the registry tracks the union of the frozen `EVENT_TYPES` plus any extra
 * string type a subscriber asks for (steward/notifications/cash/revenue kinds
 * are plain strings on the shared `events` table), and re-registers them all
 * after every reconnect.
 *
 * `contracts.ts` EVENTS_BASE points DIRECTLY at http://127.0.0.1:8485
 * (Grok product default; Omni sibling uses :8484), bypassing the same-origin
 * Next proxy on purpose (a proxied EventSource leaks upstream relays inside
 * the Node process). That behaviour is preserved verbatim here.
 */

import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { EVENTS_BASE, EVENT_TYPES, type EventRow } from "./contracts";

const LAST_ID_KEY = "omniagentos:lastEventId";
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

/** One consumer of the shared stream. `types === null` means "every frame". */
export interface EventStreamSubscriber {
  types: ReadonlySet<string> | null;
  onRow: (row: EventRow) => void;
  /** A `resync` frame arrived: drop buffered rows, adopt the synthetic row. */
  onResync: (row: EventRow) => void;
  onConnected: (connected: boolean, error?: boolean) => void;
}

/**
 * The per-tab stream owner. Created once by `EventStreamProvider`; consumers
 * only ever call `subscribe`.
 */
export class EventStreamManager {
  private es: EventSource | null = null;
  private subs = new Set<EventStreamSubscriber>();
  /** String frame kinds beyond the frozen EVENT_TYPES that subscribers want. */
  private extraTypes = new Set<string>();
  private handlers = new Map<string, (ev: MessageEvent) => void>();
  private lastId = 0;
  private connected = false;
  private error = false;
  private closed = true;
  private started = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  /** Open the single connection. Idempotent. */
  start(): void {
    if (this.started) return;
    this.started = true;
    this.closed = false;
    this.lastId = readStoredLastId();
    this.connect();
  }

  /** Close the connection and stop reconnecting. Subscribers are kept. */
  stop(): void {
    this.started = false;
    this.closed = true;
    if (this.reconnectTimer != null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.teardownListeners();
    this.es?.close();
    this.es = null;
    this.setConnected(false, false);
  }

  isConnected(): boolean {
    return this.connected;
  }

  subscribe(sub: EventStreamSubscriber): () => void {
    this.subs.add(sub);
    if (sub.types) {
      let added = false;
      for (const type of sub.types) {
        if (this.isKnownType(type)) continue;
        this.extraTypes.add(type);
        added = true;
      }
      if (added) this.syncListeners();
    }
    // Hand the newcomer the current connection state immediately.
    sub.onConnected(this.connected, this.error);
    return () => {
      this.subs.delete(sub);
    };
  }

  private isKnownType(type: string): boolean {
    return (EVENT_TYPES as readonly string[]).includes(type) || this.extraTypes.has(type);
  }

  private allTypes(): string[] {
    return [...new Set<string>([...EVENT_TYPES, ...this.extraTypes])];
  }

  private setConnected(next: boolean, error: boolean): void {
    if (this.connected === next && this.error === error) return;
    this.connected = next;
    this.error = error;
    for (const sub of this.subs) sub.onConnected(next, error);
  }

  // `eventName` is the SSE event name the frame arrived under. It is required,
  // not cosmetic: the two SSE-ONLY synthesized types carry NO `type` key in their
  // JSON body -- worker.heartbeat is {worker_id, current_run_id} and
  // session.updated is {session_id, source, state, ...} (see
  // omniagentos/api/routes/control.py _session_updated_frame). Filtering on
  // `row.type` alone yields "undefined" for both, so every typed subscriber
  // silently dropped them -- a parity break against the frozen useEvents.ts,
  // which subscribes per event NAME and never consults the body.
  private handleRow = (row: EventRow, eventName: string): void => {
    if (typeof row.id === "number" && row.id > this.lastId) {
      this.lastId = row.id;
      writeStoredLastId(row.id);
    }
    const effectiveType = row.type == null ? eventName : String(row.type);
    for (const sub of this.subs) {
      if (sub.types && !sub.types.has(effectiveType)) continue;
      sub.onRow(row);
    }
  };

  private handleResync = (data: string): void => {
    let latestId = 0;
    try {
      const payload = JSON.parse(data) as { latest_id?: number };
      latestId = typeof payload.latest_id === "number" ? payload.latest_id : 0;
    } catch {
      latestId = 0;
    }
    this.lastId = latestId;
    writeStoredLastId(latestId);
    const synthetic = makeResyncRow(latestId);
    // Every subscriber clears its buffer — the server has told us our cursor is
    // no longer valid, so retained rows may be stale for any type filter.
    for (const sub of this.subs) sub.onResync(synthetic);
  };

  private teardownListeners(): void {
    if (this.es) {
      for (const [type, handler] of this.handlers) {
        this.es.removeEventListener(type, handler as EventListener);
      }
    }
    this.handlers.clear();
  }

  /** Register a listener for every type we care about that lacks one. */
  private syncListeners(): void {
    const es = this.es;
    if (!es) return;
    for (const type of this.allTypes()) {
      if (this.handlers.has(type)) continue;
      const handler = (ev: MessageEvent) => {
        if (this.closed) return;
        const row = parseEventRow(ev.data);
        if (row) this.handleRow(row, type);
      };
      this.handlers.set(type, handler);
      es.addEventListener(type, handler as EventListener);
    }
    if (!this.handlers.has("resync")) {
      const handler = (ev: MessageEvent) => {
        if (this.closed) return;
        this.handleResync(ev.data);
      };
      this.handlers.set("resync", handler);
      es.addEventListener("resync", handler as EventListener);
    }
  }

  private connect = (): void => {
    if (this.closed) return;
    if (typeof EventSource === "undefined") return;

    // No `types` param on purpose: one connection serves every subscriber and
    // filtering happens client-side.
    const params = new URLSearchParams();
    params.set("after_id", String(this.lastId));

    const es = new EventSource(`${EVENTS_BASE}/api/events?${params.toString()}`);
    this.es = es;

    es.onopen = () => {
      if (this.closed) return;
      this.setConnected(true, false);
    };

    es.onerror = () => {
      if (this.closed) return;
      this.setConnected(false, true);
      // Close and re-open with the latest after_id so the frozen query-param
      // cursor (not a stale URL) is used after reconnect.
      this.teardownListeners();
      es.close();
      if (this.es === es) this.es = null;
      if (this.reconnectTimer != null) clearTimeout(this.reconnectTimer);
      this.reconnectTimer = setTimeout(this.connect, RECONNECT_MS);
    };

    // Fallback if the server ever emits an untyped frame. Per the SSE spec an
    // event with no `event:` field arrives as "message", so that is the correct
    // name to attribute it to; such a frame carries its own `type` in the body
    // anyway, which takes precedence in handleRow.
    es.onmessage = (ev: MessageEvent) => {
      if (this.closed) return;
      const row = parseEventRow(ev.data);
      if (row) this.handleRow(row, "message");
    };

    this.handlers.clear();
    this.syncListeners();
  };
}

const EventStreamContext = createContext<EventStreamManager | null>(null);

/** Internal: `useEventChannel` reads the manager through this. */
export function useEventStreamManager(): EventStreamManager | null {
  return useContext(EventStreamContext);
}

/**
 * Mount ONCE, in `app/layout.tsx`, above everything that consumes events.
 */
export function EventStreamProvider({ children }: { children: ReactNode }) {
  const managerRef = useRef<EventStreamManager | null>(null);
  if (managerRef.current === null) managerRef.current = new EventStreamManager();
  const manager = managerRef.current;
  // Keeps the context value referentially stable for the tree's lifetime.
  const [value] = useState(manager);

  useEffect(() => {
    manager.start();
    return () => manager.stop();
  }, [manager]);

  return <EventStreamContext.Provider value={value}>{children}</EventStreamContext.Provider>;
}
