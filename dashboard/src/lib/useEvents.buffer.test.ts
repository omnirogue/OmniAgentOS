import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { EventRow, EventType } from "./contracts";
import { useEvents } from "./useEvents";

/**
 * The event buffer belongs to ONE filter.
 *
 * `useEvents` reconnects with a new EventSource when its `types` filter
 * changes, but the rows it already collected are React state that nothing
 * removes. Left alone, a list switched from board events to revenue events
 * keeps rendering board rows until enough revenue events arrive to push them
 * out one at a time — a filtered view showing rows that do not match the
 * filter, with no way for the user to tell.
 */

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  url: string;
  readyState = 0;
  onopen: ((this: EventSource, ev: Event) => void) | null = null;
  onerror: ((this: EventSource, ev: Event) => void) | null = null;
  onmessage: ((this: EventSource, ev: MessageEvent) => void) | null = null;
  private listeners = new Map<string, ((ev: MessageEvent) => void)[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (ev: MessageEvent) => void): void {
    const existing = this.listeners.get(type) ?? [];
    existing.push(listener);
    this.listeners.set(type, existing);
  }

  removeEventListener(): void {}

  close(): void {
    this.readyState = 2;
  }

  open(): void {
    this.readyState = 1;
    this.onopen?.call(this as unknown as EventSource, new Event("open"));
  }

  emit(row: EventRow): void {
    const event = new MessageEvent(row.type, { data: JSON.stringify(row) });
    for (const listener of this.listeners.get(row.type) ?? []) listener(event);
  }
}

function row(id: number, type: EventType): EventRow {
  return {
    id,
    ts: "2026-08-04T00:00:00Z",
    type,
    actor: "api",
    action: "updated",
    target_type: "",
    target_id: "",
    payload: {},
    trace_id: "",
  };
}

describe("useEvents buffer", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    sessionStorage.clear();
    (globalThis as unknown as { EventSource: unknown }).EventSource = FakeEventSource;
  });

  afterEach(() => {
    delete (globalThis as unknown as { EventSource?: unknown }).EventSource;
  });

  it("drops the previous filter's rows when the filter changes", async () => {
    const { result, rerender } = renderHook(({ types }) => useEvents(types), {
      initialProps: { types: ["board.updated"] as EventType[] },
    });

    act(() => {
      FakeEventSource.instances[0].open();
      FakeEventSource.instances[0].emit(row(1, "board.updated"));
    });
    await waitFor(() => expect(result.current.events).toHaveLength(1));

    rerender({ types: ["run.updated"] as EventType[] });

    await waitFor(() => expect(result.current.events).toEqual([]));
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.instances[1].url).toContain("types=run.updated");
  });

  it("keeps collecting on the new filter after the clear", async () => {
    const { result, rerender } = renderHook(({ types }) => useEvents(types), {
      initialProps: { types: ["board.updated"] as EventType[] },
    });
    act(() => {
      FakeEventSource.instances[0].open();
      FakeEventSource.instances[0].emit(row(1, "board.updated"));
    });
    await waitFor(() => expect(result.current.events).toHaveLength(1));

    rerender({ types: ["run.updated"] as EventType[] });
    act(() => {
      FakeEventSource.instances[1].open();
      FakeEventSource.instances[1].emit(row(2, "run.updated"));
    });

    await waitFor(() => expect(result.current.events.map((e) => e.id)).toEqual([2]));
  });

  it("does not clear rows on a re-render that leaves the filter alone", async () => {
    const { result, rerender } = renderHook(({ types }) => useEvents(types), {
      initialProps: { types: ["board.updated"] as EventType[] },
    });
    act(() => {
      FakeEventSource.instances[0].open();
      FakeEventSource.instances[0].emit(row(1, "board.updated"));
    });
    await waitFor(() => expect(result.current.events).toHaveLength(1));

    // A fresh array literal with the same contents: the hook keys on the joined
    // types, so this must neither reconnect nor drop what it collected.
    rerender({ types: ["board.updated"] as EventType[] });

    expect(result.current.events.map((e) => e.id)).toEqual([1]);
    expect(FakeEventSource.instances).toHaveLength(1);
  });
});
