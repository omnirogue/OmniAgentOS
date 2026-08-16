"use client";

/**
 * Drop-in replacement for the FROZEN `useEvents` that costs ZERO extra
 * connections (T1.4).
 *
 * Same return shape (`UseEventsResult`, imported from the frozen module so the
 * two can never drift), same buffering/dedup/cap semantics, same `resync`
 * behaviour — but instead of opening its own `EventSource` it subscribes to the
 * single per-tab stream owned by `EventStreamProvider` and filters frames
 * client-side. Migrating a call site is a one-line import swap.
 *
 * `useEvents` itself is FROZEN (Wave 0) and stays exactly as-is: it remains the
 * documented contract and the fallback for any tree without a provider.
 *
 * Type filter: accepts the frozen `EventType`s AND plain strings, because the
 * steward / notifications / cash / revenue frames (`alert.created`,
 * `briefing.ready`, `bank.updated`, …) ride the same `events` table without
 * being part of the frozen `EVENT_TYPES` tuple.
 */

import { useEffect, useRef, useState } from "react";
import { useEventStreamManager } from "./EventStreamProvider";
import type { EventRow, EventType } from "./contracts";
import type { UseEventsResult } from "./useEvents";

function appendCapped(prev: EventRow[], row: EventRow, maxBuffer: number): EventRow[] {
  // Prefer newest; drop oldest when over capacity.
  if (prev.some((e) => e.id === row.id && e.type === row.type)) {
    return prev;
  }
  const next = [...prev, row];
  if (next.length <= maxBuffer) return next;
  return next.slice(next.length - maxBuffer);
}

let warnedMissingProvider = false;

/**
 * Subscribe to the shared event stream.
 *
 * @param types  frame kinds to keep; omitted/empty = every frame
 * @param maxBuffer cap on retained rows (default 200, same as `useEvents`)
 */
export type UseEventChannelResult = UseEventsResult & { error: boolean };

export function useEventChannel(
  types?: readonly (EventType | string)[],
  maxBuffer = 200,
): UseEventChannelResult {
  const manager = useEventStreamManager();
  const [events, setEvents] = useState<EventRow[]>([]);
  const [lastEvent, setLastEvent] = useState<EventRow | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState(false);
  const maxBufferRef = useRef(maxBuffer);
  maxBufferRef.current = maxBuffer;

  // Stable key so parent re-renders with a fresh array literal don't thrash the
  // subscription (same trick the frozen useEvents uses for the socket).
  const typesKey = types?.length ? [...types].join(",") : "";

  useEffect(() => {
    if (!manager) {
      // Developer error: the provider belongs in app/layout.tsx. Loud in dev,
      // inert (never a white screen) in production.
      const message =
        "useEventChannel: no <EventStreamProvider> above this component. " +
        "Mount it in app/layout.tsx, or use the frozen useEvents() hook instead.";
      if (process.env.NODE_ENV !== "production") throw new Error(message);
      if (!warnedMissingProvider) {
        warnedMissingProvider = true;
        console.error(message);
      }
      return;
    }

    const wanted = typesKey ? new Set(typesKey.split(",")) : null;

    const unsubscribe = manager.subscribe({
      types: wanted,
      onRow: (row) => {
        setEvents((prev) => appendCapped(prev, row, maxBufferRef.current));
        setLastEvent(row);
      },
      onResync: (synthetic) => {
        setEvents([]);
        setLastEvent(synthetic);
      },
      onConnected: (c, e) => {
        setConnected(c);
        if (e !== undefined) setError(e);
      },
    });

    return () => {
      unsubscribe();
    };
  }, [manager, typesKey]);

  return { events, lastEvent, connected, error };
}
