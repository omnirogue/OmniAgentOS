"use client";

/**
 * SSE hook for V2 reliability event types plus L12 event-hub status.
 * It intentionally remains separate from the shared useEvents hook so its
 * reliability-specific payloads and cursor can evolve additively.
 */

import { useEffect, useRef, useState } from "react";
import { EVENTS_BASE } from "./contracts";
import {
  EVENTBUS_STATUS_EVENT,
  RELIABILITY_EVENT_TYPES,
  type EventBusStatusEvent,
  type ReliabilityEventType,
  type ReliabilitySseRow,
} from "./reliabilityContracts";

export interface UseReliabilityEventsResult {
  events: ReliabilitySseRow[];
  lastEvent: ReliabilitySseRow | null;
  connected: boolean;
  error: boolean;
  /** Latest L12 `event: eventbus.status` frame (null until received). */
  eventBusStatus: EventBusStatusEvent | null;
}

interface ConnectionAuthority {
  /** Source configuration that owns every value in this snapshot. */
  ownerKey: string;
  connected: boolean;
  error: boolean;
  eventBusStatus: EventBusStatusEvent | null;
}

const LAST_ID_KEY = "omniagentos:reliability:lastEventId";
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

function parseEventRow(data: string): ReliabilitySseRow | null {
  try {
    const row = JSON.parse(data) as ReliabilitySseRow;
    if (row == null || typeof row !== "object") return null;
    return row;
  } catch {
    return null;
  }
}

function makeResyncRow(latestId: number): ReliabilitySseRow {
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
  prev: ReliabilitySseRow[],
  row: ReliabilitySseRow,
  maxBuffer: number,
): ReliabilitySseRow[] {
  if (prev.some((e) => e.id === row.id && e.type === row.type)) {
    return prev;
  }
  const next = [...prev, row];
  if (next.length <= maxBuffer) return next;
  return next.slice(next.length - maxBuffer);
}

function isReliabilityType(type: string): boolean {
  return (RELIABILITY_EVENT_TYPES as readonly string[]).includes(type);
}

function parseEventBusStatus(data: string): EventBusStatusEvent | null {
  try {
    const row = JSON.parse(data) as EventBusStatusEvent;
    if (row == null || typeof row !== "object") return null;
    const state = typeof row.state === "string" ? row.state : null;
    if (!state) return null;
    return {
      contract_version:
        typeof row.contract_version === "number" ? row.contract_version : undefined,
      type: typeof row.type === "string" ? row.type : EVENTBUS_STATUS_EVENT,
      state,
      reason: typeof row.reason === "string" ? row.reason : row.reason === null ? null : undefined,
      degraded: row.degraded === true || state === "degraded",
      consecutive_failures:
        typeof row.consecutive_failures === "number" ? row.consecutive_failures : null,
      last_error:
        typeof row.last_error === "string" ? row.last_error : row.last_error === null ? null : undefined,
      tailer_restarts:
        typeof row.tailer_restarts === "number" ? row.tailer_restarts : null,
      max_tailer_restarts:
        typeof row.max_tailer_restarts === "number" ? row.max_tailer_restarts : null,
      ts: typeof row.ts === "number" ? row.ts : null,
    };
  } catch {
    return null;
  }
}

/**
 * Subscribe to V2 reliability SSE frames + L12 `eventbus.status`.
 * @param types subset of RELIABILITY_EVENT_TYPES (default: all V2 types)
 * @param maxBuffer cap on retained rows
 * @param enabled when false, never open EventSource and stop reconnect (H-16 latch)
 */
export function useReliabilityEvents(
  types?: ReliabilityEventType[],
  maxBuffer = 200,
  enabled = true,
): UseReliabilityEventsResult {
  const [events, setEvents] = useState<ReliabilitySseRow[]>([]);
  const [lastEvent, setLastEvent] = useState<ReliabilitySseRow | null>(null);
  const lastIdRef = useRef<number>(0);
  const maxBufferRef = useRef(maxBuffer);
  maxBufferRef.current = maxBuffer;

  const typesKey = types?.length ? types.join(",") : RELIABILITY_EVENT_TYPES.join(",");
  // Authority is bound to the complete source configuration. A dependency
  // replacement is visible during render before the old effect cleanup/new
  // setup runs, so owner-tagging masks its connected/status evidence
  // synchronously without scheduling state from an unmount cleanup.
  const ownerKey = `${enabled ? "enabled" : "disabled"}\u0000${EVENTS_BASE}\u0000${typesKey}`;
  const [authority, setAuthority] = useState<ConnectionAuthority>({
    ownerKey: "",
    connected: false,
    error: false,
    eventBusStatus: null,
  });
  const ownsAuthority = authority.ownerKey === ownerKey;
  const connected = ownsAuthority ? authority.connected : false;
  const error = ownsAuthority ? authority.error : false;
  const eventBusStatus = ownsAuthority ? authority.eventBusStatus : null;

  useEffect(() => {
    const disconnectedAuthority = (): ConnectionAuthority => ({
      ownerKey,
      connected: false,
      error: false,
      eventBusStatus: null,
    });
    setAuthority(disconnectedAuthority());

    if (!enabled) {
      return;
    }

    lastIdRef.current = readStoredLastId();

    let closed = false;
    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let connectionEpoch = 0;

    const listenTypes = typesKey.split(",").filter(Boolean);

    const publishAuthority = (
      update: (current: ConnectionAuthority) => ConnectionAuthority,
    ) => {
      setAuthority((current) =>
        update(
          current.ownerKey === ownerKey
            ? current
            : disconnectedAuthority(),
        ),
      );
    };

    const handleRow = (row: ReliabilitySseRow) => {
      // Only surface V2 reliability types (ignore stray frames).
      if (row.type !== "resync" && !isReliabilityType(row.type)) return;
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
      const epoch = ++connectionEpoch;

      const params = new URLSearchParams();
      params.set("after_id", String(lastIdRef.current));
      // Always include L12 eventbus.status alongside requested reliability types.
      const typesWithHub = Array.from(
        new Set([...listenTypes, EVENTBUS_STATUS_EVENT]),
      ).join(",");
      params.set("types", typesWithHub);

      const url = `${EVENTS_BASE}/api/events?${params.toString()}`;
      const source = new EventSource(url);
      es = source;

      source.onopen = () => {
        if (!closed && epoch === connectionEpoch) {
          publishAuthority((current) => ({
            ...current,
            connected: true,
            error: false,
          }));
        }
      };

      source.onerror = () => {
        if (closed || epoch !== connectionEpoch) return;
        // Invalidate this source before any state update or queued callback can
        // run. EventSource.close() prevents future network work, but callbacks
        // already queued by the browser can still be delivered.
        const reconnectEpoch = ++connectionEpoch;
        // A status frame is authoritative only for the connection epoch that
        // delivered it. Polling must take over until this reconnect receives a
        // fresh frame, otherwise an old "ok" can hide newer degraded health.
        publishAuthority((current) => ({
          ...current,
          connected: false,
          error: true,
          eventBusStatus: null,
        }));
        source.close();
        if (es === source) es = null;
        if (reconnectTimer != null) clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(() => {
          if (closed || reconnectEpoch !== connectionEpoch || es !== null) {
            return;
          }
          reconnectTimer = null;
          connect();
        }, RECONNECT_MS);
      };

      for (const type of listenTypes) {
        source.addEventListener(type, (ev: MessageEvent) => {
          if (closed || epoch !== connectionEpoch) return;
          const row = parseEventRow(ev.data);
          if (row) handleRow(row);
        });
      }

      // L12 first-class degraded signal (named SSE event, not a reliability row).
      source.addEventListener(EVENTBUS_STATUS_EVENT, (ev: MessageEvent) => {
        if (closed || epoch !== connectionEpoch) return;
        const status = parseEventBusStatus(ev.data);
        if (status) {
          publishAuthority((current) => ({
            ...current,
            eventBusStatus: status,
          }));
        }
      });

      source.addEventListener("resync", (ev: MessageEvent) => {
        if (closed || epoch !== connectionEpoch) return;
        handleResync(ev.data);
      });

      source.onmessage = (ev: MessageEvent) => {
        if (closed || epoch !== connectionEpoch) return;
        const row = parseEventRow(ev.data);
        if (row) handleRow(row);
        // Some hubs may emit untyped frames with type in the body.
        if (row?.type === EVENTBUS_STATUS_EVENT) {
          const status = parseEventBusStatus(ev.data);
          if (status) {
            publishAuthority((current) => ({
              ...current,
              eventBusStatus: status,
            }));
          }
        }
      };
    };

    connect();

    return () => {
      closed = true;
      connectionEpoch += 1;
      if (reconnectTimer != null) clearTimeout(reconnectTimer);
      es?.close();
      es = null;
    };
  }, [typesKey, enabled, ownerKey]);

  return { events, lastEvent, connected, error, eventBusStatus };
}
