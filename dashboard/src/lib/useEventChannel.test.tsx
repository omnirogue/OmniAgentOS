import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EventStreamProvider } from "./EventStreamProvider";
import { useEventChannel } from "./useEventChannel";
import type { EventRow } from "./contracts";

/** Minimal EventSource double: records every instance so the test can assert
 * that a whole tree opens exactly ONE connection (the point of T1.4). */
class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  closed = false;
  onopen: ((ev: Event) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  private listeners = new Map<string, Set<(ev: MessageEvent) => void>>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, handler: (ev: MessageEvent) => void) {
    const set = this.listeners.get(type) ?? new Set();
    set.add(handler);
    this.listeners.set(type, set);
  }

  removeEventListener(type: string, handler: (ev: MessageEvent) => void) {
    this.listeners.get(type)?.delete(handler);
  }

  close() {
    this.closed = true;
  }

  hasListener(type: string): boolean {
    return (this.listeners.get(type)?.size ?? 0) > 0;
  }

  emit(type: string, payload: unknown) {
    const ev = { data: JSON.stringify(payload) } as MessageEvent;
    for (const handler of this.listeners.get(type) ?? []) handler(ev);
  }
}

function row(id: number, type: string): EventRow {
  return {
    id,
    ts: "2026-01-01T00:00:00Z",
    type,
    actor: "test",
    action: "x",
    target_type: "",
    target_id: "",
    payload: {},
    trace_id: "",
  };
}

function Probe({ label, types }: { label: string; types?: string[] }) {
  const { events, lastEvent, connected, error } = useEventChannel(types);
  return (
    <div>
      <span data-testid={`${label}-count`}>{events.length}</span>
      <span data-testid={`${label}-last`}>{lastEvent ? `${lastEvent.type}:${lastEvent.id}` : "none"}</span>
      <span data-testid={`${label}-connected`}>{String(connected)}</span>
      <span data-testid={`${label}-error`}>{String(error)}</span>
    </div>
  );
}

describe("useEventChannel + EventStreamProvider", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
    sessionStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("opens exactly one EventSource for many subscribers and filters per-type", () => {
    render(
      <EventStreamProvider>
        <Probe label="pause" types={["pause.changed"]} />
        <Probe label="alerts" types={["alert.created"]} />
        <Probe label="all" />
      </EventStreamProvider>,
    );

    expect(FakeEventSource.instances).toHaveLength(1);
    const es = FakeEventSource.instances[0];
    expect(es.url).toContain("http://127.0.0.1:8485/api/events?after_id=0");
    // No server-side `types` filter: one stream serves every subscriber.
    expect(es.url).not.toContain("types=");
    // Non-frozen string kinds requested by a subscriber get a listener too.
    expect(es.hasListener("alert.created")).toBe(true);

    act(() => es.onopen?.(new Event("open")));
    expect(screen.getByTestId("pause-connected")).toHaveTextContent("true");
    expect(screen.getByTestId("pause-error")).toHaveTextContent("false");

    act(() => es.emit("pause.changed", row(7, "pause.changed")));
    act(() => es.emit("alert.created", row(8, "alert.created")));

    expect(screen.getByTestId("pause-count")).toHaveTextContent("1");
    expect(screen.getByTestId("pause-last")).toHaveTextContent("pause.changed:7");
    expect(screen.getByTestId("alerts-count")).toHaveTextContent("1");
    expect(screen.getByTestId("all-count")).toHaveTextContent("2");
    // Cursor advanced on the shared sessionStorage key (frozen useEvents semantics).
    expect(sessionStorage.getItem("omniagentos:lastEventId")).toBe("8");
  });

  it("clears every subscriber's buffer on a resync frame", () => {
    render(
      <EventStreamProvider>
        <Probe label="pause" types={["pause.changed"]} />
      </EventStreamProvider>,
    );
    const es = FakeEventSource.instances[0];
    act(() => es.emit("pause.changed", row(3, "pause.changed")));
    expect(screen.getByTestId("pause-count")).toHaveTextContent("1");

    act(() => es.emit("resync", { latest_id: 42 }));
    expect(screen.getByTestId("pause-count")).toHaveTextContent("0");
    expect(screen.getByTestId("pause-last")).toHaveTextContent("resync:42");
    expect(sessionStorage.getItem("omniagentos:lastEventId")).toBe("42");
  });

  it("reconnects with the latest after_id cursor and sets error", () => {
    vi.useFakeTimers();
    try {
      render(
        <EventStreamProvider>
          <Probe label="all" />
        </EventStreamProvider>,
      );
      const es = FakeEventSource.instances[0];
      act(() => es.emit("run.updated", row(11, "run.updated")));
      act(() => es.onerror?.(new Event("error")));
      expect(es.closed).toBe(true);
      expect(screen.getByTestId("all-connected")).toHaveTextContent("false");
      expect(screen.getByTestId("all-error")).toHaveTextContent("true");

      act(() => vi.advanceTimersByTime(1500));
      expect(FakeEventSource.instances).toHaveLength(2);
      expect(FakeEventSource.instances[1].url).toContain("after_id=11");
    } finally {
      vi.useRealTimers();
    }
  });

  it("closes the connection when the provider unmounts", () => {
    const view = render(
      <EventStreamProvider>
        <Probe label="all" />
      </EventStreamProvider>,
    );
    const es = FakeEventSource.instances[0];
    view.unmount();
    expect(es.closed).toBe(true);
  });
});
