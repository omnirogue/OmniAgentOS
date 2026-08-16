import { act, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useReliabilityEvents } from "./useReliabilityEvents";
import type { ReliabilityEventType } from "./reliabilityContracts";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  closed = false;
  onopen: ((ev: Event) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  listeners = new Map<string, (ev: MessageEvent) => void>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject) {
    if (typeof listener === "function") {
      this.listeners.set(type, listener as (ev: MessageEvent) => void);
    }
  }
  removeEventListener() {}
  close() {
    this.closed = true;
  }
  emit(type: string, data: Record<string, unknown>) {
    this.listeners.get(type)?.(
      new MessageEvent(type, { data: JSON.stringify(data) }),
    );
  }
}

function Probe({
  enabled,
  types,
}: {
  enabled: boolean;
  types?: ReliabilityEventType[];
}) {
  const { connected, error, eventBusStatus, lastEvent } = useReliabilityEvents(
    types,
    200,
    enabled,
  );
  return (
    <div>
      <span data-testid="connected">{String(connected)}</span>
      <span data-testid="error">{String(error)}</span>
      <span data-testid="hub-status">{eventBusStatus?.state ?? "none"}</span>
      <span data-testid="last-event">{lastEvent?.id ?? "none"}</span>
      <span data-testid="sources">{FakeEventSource.instances.length}</span>
    </div>
  );
}

describe("useReliabilityEvents enabled latch (H-16)", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
    sessionStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("opens EventSource when enabled and requests L12 eventbus.status", () => {
    render(<Probe enabled />);
    expect(FakeEventSource.instances).toHaveLength(1);
    const url = FakeEventSource.instances[0].url;
    expect(url).toContain("http://127.0.0.1:8485/api/events");
    expect(url).toContain("eventbus.status");
  });

  it("opens no EventSource when disabled (fail-closed latch)", () => {
    render(<Probe enabled={false} />);
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(screen.getByTestId("connected")).toHaveTextContent("false");
  });

  it("closes the stream when enabled flips to false", () => {
    const view = render(<Probe enabled />);
    expect(FakeEventSource.instances).toHaveLength(1);
    const first = FakeEventSource.instances[0];
    view.rerender(<Probe enabled={false} />);
    expect(first.closed).toBe(true);
  });

  it("surfaces stream error on onerror until reopen", () => {
    render(<Probe enabled />);
    const es = FakeEventSource.instances[0];
    act(() => es.onerror?.(new Event("error")));
    expect(screen.getByTestId("connected")).toHaveTextContent("false");
    expect(screen.getByTestId("error")).toHaveTextContent("true");
  });

  it("invalidates status across reconnect and accepts only a fresh epoch frame", () => {
    vi.useFakeTimers();
    try {
      render(<Probe enabled />);
      const first = FakeEventSource.instances[0];
      act(() => {
        first.onopen?.(new Event("open"));
        first.emit("eventbus.status", {
          contract_version: 1,
          type: "eventbus.status",
          state: "ok",
          degraded: false,
        });
      });
      expect(screen.getByTestId("hub-status")).toHaveTextContent("ok");

      act(() => first.onerror?.(new Event("error")));
      expect(screen.getByTestId("hub-status")).toHaveTextContent("none");

      // Closing a source does not cancel callbacks already queued by the
      // browser. It must be stale for the entire reconnect delay, not only
      // after the replacement source is constructed.
      act(() => {
        first.onopen?.(new Event("open"));
        first.emit("eventbus.status", {
          contract_version: 1,
          type: "eventbus.status",
          state: "ok",
          degraded: false,
        });
        first.emit("resync", { latest_id: 91 });
        first.emit("audit.completed", {
          id: 92,
          type: "audit.completed",
        });
      });
      expect(screen.getByTestId("connected")).toHaveTextContent("false");
      expect(screen.getByTestId("error")).toHaveTextContent("true");
      expect(screen.getByTestId("hub-status")).toHaveTextContent("none");
      expect(screen.getByTestId("last-event")).toHaveTextContent("none");

      act(() => vi.advanceTimersByTime(1500));
      expect(FakeEventSource.instances).toHaveLength(2);
      const second = FakeEventSource.instances[1];
      act(() => second.onopen?.(new Event("open")));
      expect(screen.getByTestId("connected")).toHaveTextContent("true");
      expect(screen.getByTestId("hub-status")).toHaveTextContent("none");

      act(() => {
        first.emit("eventbus.status", {
          state: "degraded",
          degraded: true,
        });
      });
      expect(screen.getByTestId("hub-status")).toHaveTextContent("none");

      act(() => {
        second.emit("eventbus.status", {
          contract_version: 1,
          type: "eventbus.status",
          state: "ok",
          reason: "tailer_recovered",
          degraded: false,
        });
      });
      expect(screen.getByTestId("hub-status")).toHaveTextContent("ok");
    } finally {
      vi.useRealTimers();
    }
  });

  it("makes reconnect scheduling idempotent across repeated errors and unmount", () => {
    vi.useFakeTimers();
    try {
      const view = render(<Probe enabled />);
      const first = FakeEventSource.instances[0];

      act(() => {
        first.onerror?.(new Event("error"));
        first.onerror?.(new Event("error"));
      });
      expect(first.closed).toBe(true);
      act(() => vi.advanceTimersByTime(500));
      expect(FakeEventSource.instances).toHaveLength(1);
      act(() => vi.advanceTimersByTime(999));
      expect(FakeEventSource.instances).toHaveLength(1);
      act(() => vi.advanceTimersByTime(1));
      expect(FakeEventSource.instances).toHaveLength(2);

      const second = FakeEventSource.instances[1];
      act(() => {
        second.onopen?.(new Event("open"));
        second.onerror?.(new Event("error"));
        second.onerror?.(new Event("error"));
        // An old epoch cannot replace or delay the current epoch's timer.
        first.onerror?.(new Event("error"));
      });
      act(() => vi.advanceTimersByTime(1500));
      expect(FakeEventSource.instances).toHaveLength(3);

      const third = FakeEventSource.instances[2];
      view.unmount();
      expect(third.closed).toBe(true);
      act(() => {
        third.onerror?.(new Event("error"));
        third.onopen?.(new Event("open"));
        third.emit("eventbus.status", {
          state: "ok",
          degraded: false,
        });
        vi.advanceTimersByTime(3000);
      });
      expect(FakeEventSource.instances).toHaveLength(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("invalidates status across disable and accepts only the re-enabled source", () => {
    const view = render(<Probe enabled />);
    const first = FakeEventSource.instances[0];
    act(() => {
      first.onopen?.(new Event("open"));
      first.emit("eventbus.status", {
        contract_version: 1,
        type: "eventbus.status",
        state: "ok",
        degraded: false,
      });
    });
    expect(screen.getByTestId("hub-status")).toHaveTextContent("ok");

    view.rerender(<Probe enabled={false} />);
    expect(first.closed).toBe(true);
    expect(screen.getByTestId("connected")).toHaveTextContent("false");
    expect(screen.getByTestId("error")).toHaveTextContent("false");
    expect(screen.getByTestId("hub-status")).toHaveTextContent("none");

    // Closing a source cannot cancel callbacks already queued by the browser.
    act(() => {
      first.onopen?.(new Event("open"));
      first.emit("eventbus.status", {
        state: "ok",
        degraded: false,
      });
      first.emit("resync", { latest_id: 101 });
      first.emit("audit.completed", {
        id: 102,
        type: "audit.completed",
      });
      first.onerror?.(new Event("error"));
    });
    expect(screen.getByTestId("hub-status")).toHaveTextContent("none");
    expect(screen.getByTestId("last-event")).toHaveTextContent("none");

    view.rerender(<Probe enabled />);
    expect(FakeEventSource.instances).toHaveLength(2);
    const second = FakeEventSource.instances[1];
    act(() => second.onopen?.(new Event("open")));
    expect(screen.getByTestId("connected")).toHaveTextContent("true");
    expect(screen.getByTestId("hub-status")).toHaveTextContent("none");

    act(() => {
      second.emit("eventbus.status", {
        state: "degraded",
        degraded: true,
      });
    });
    expect(screen.getByTestId("hub-status")).toHaveTextContent("degraded");
  });

  it("invalidates status and callbacks when subscription types replace a source", () => {
    const view = render(
      <Probe enabled types={["audit.completed"]} />,
    );
    const first = FakeEventSource.instances[0];
    act(() => {
      first.onopen?.(new Event("open"));
      first.emit("eventbus.status", {
        contract_version: 1,
        type: "eventbus.status",
        state: "ok",
        degraded: false,
      });
    });
    expect(screen.getByTestId("hub-status")).toHaveTextContent("ok");

    view.rerender(
      <Probe enabled types={["reliability.event"]} />,
    );
    expect(first.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(screen.getByTestId("connected")).toHaveTextContent("false");
    expect(screen.getByTestId("hub-status")).toHaveTextContent("none");

    const second = FakeEventSource.instances[1];
    act(() => {
      first.onopen?.(new Event("open"));
      first.emit("eventbus.status", {
        state: "ok",
        degraded: false,
      });
      first.emit("resync", { latest_id: 111 });
      first.emit("audit.completed", {
        id: 112,
        type: "audit.completed",
      });
      first.onerror?.(new Event("error"));
      second.onopen?.(new Event("open"));
    });
    expect(screen.getByTestId("connected")).toHaveTextContent("true");
    expect(screen.getByTestId("hub-status")).toHaveTextContent("none");
    expect(screen.getByTestId("last-event")).toHaveTextContent("none");

    act(() => {
      second.emit("eventbus.status", {
        state: "ok",
        reason: "tailer_recovered",
        degraded: false,
      });
    });
    expect(screen.getByTestId("hub-status")).toHaveTextContent("ok");
  });

  it("rejects callbacks queued by an effect-replaced source", () => {
    vi.useFakeTimers();
    try {
      const view = render(
        <Probe enabled types={["audit.completed"]} />,
      );
      const first = FakeEventSource.instances[0];
      act(() => {
        first.onopen?.(new Event("open"));
        first.emit("eventbus.status", {
          state: "ok",
          degraded: false,
        });
      });

      setTimeout(() => {
        first.onopen?.(new Event("open"));
        first.emit("eventbus.status", {
          state: "ok",
          degraded: false,
        });
        first.emit("resync", { latest_id: 131 });
        first.emit("audit.completed", {
          id: 132,
          type: "audit.completed",
        });
        first.onerror?.(new Event("error"));
      }, 0);

      view.rerender(
        <Probe enabled types={["org.updated"]} />,
      );
      const second = FakeEventSource.instances[1];
      act(() => vi.advanceTimersByTime(0));

      expect(first.closed).toBe(true);
      expect(screen.getByTestId("connected")).toHaveTextContent("false");
      expect(screen.getByTestId("error")).toHaveTextContent("false");
      expect(screen.getByTestId("hub-status")).toHaveTextContent("none");
      expect(screen.getByTestId("last-event")).toHaveTextContent("none");
      expect(FakeEventSource.instances).toHaveLength(2);

      act(() => {
        second.onopen?.(new Event("open"));
        second.emit("eventbus.status", {
          state: "degraded",
          degraded: true,
        });
      });
      expect(screen.getByTestId("connected")).toHaveTextContent("true");
      expect(screen.getByTestId("hub-status")).toHaveTextContent("degraded");
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps only the final owner through rapid enable and type replacements", () => {
    const view = render(
      <Probe enabled types={["audit.completed"]} />,
    );
    const first = FakeEventSource.instances[0];
    act(() => {
      first.onopen?.(new Event("open"));
      first.emit("eventbus.status", {
        state: "ok",
        degraded: false,
      });
    });

    view.rerender(
      <Probe enabled={false} types={["audit.completed"]} />,
    );
    view.rerender(
      <Probe enabled types={["reliability.event"]} />,
    );
    const second = FakeEventSource.instances[1];
    view.rerender(
      <Probe enabled types={["org.updated"]} />,
    );
    const third = FakeEventSource.instances[2];

    expect(first.closed).toBe(true);
    expect(second.closed).toBe(true);
    expect(screen.getByTestId("connected")).toHaveTextContent("false");
    expect(screen.getByTestId("hub-status")).toHaveTextContent("none");

    act(() => {
      for (const stale of [first, second]) {
        stale.onopen?.(new Event("open"));
        stale.emit("eventbus.status", {
          state: "ok",
          degraded: false,
        });
        stale.emit("org.updated", {
          id: 121,
          type: "org.updated",
        });
        stale.onerror?.(new Event("error"));
      }
      third.onopen?.(new Event("open"));
      third.emit("eventbus.status", {
        state: "degraded",
        degraded: true,
      });
    });
    expect(screen.getByTestId("connected")).toHaveTextContent("true");
    expect(screen.getByTestId("error")).toHaveTextContent("false");
    expect(screen.getByTestId("hub-status")).toHaveTextContent("degraded");
    expect(screen.getByTestId("last-event")).toHaveTextContent("none");
    expect(FakeEventSource.instances).toHaveLength(3);
  });

  it("fences every superseded StrictMode effect during rapid owner replacement and unmount", () => {
    vi.useFakeTimers();
    try {
      const view = render(
        <StrictMode>
          <Probe enabled types={["audit.completed"]} />
        </StrictMode>,
      );
      expect(FakeEventSource.instances.length).toBeGreaterThanOrEqual(2);
      const strictDiscarded = FakeEventSource.instances[0];
      const firstOwner = FakeEventSource.instances.at(-1)!;
      expect(strictDiscarded.closed).toBe(true);

      act(() => {
        firstOwner.onopen?.(new Event("open"));
        firstOwner.emit("eventbus.status", {
          state: "ok",
          degraded: false,
        });
      });
      expect(screen.getByTestId("hub-status")).toHaveTextContent("ok");

      view.rerender(
        <StrictMode>
          <Probe enabled={false} types={["audit.completed"]} />
        </StrictMode>,
      );
      view.rerender(
        <StrictMode>
          <Probe enabled types={["reliability.event"]} />
        </StrictMode>,
      );
      const secondOwner = FakeEventSource.instances.at(-1)!;
      view.rerender(
        <StrictMode>
          <Probe enabled types={["org.updated"]} />
        </StrictMode>,
      );
      const finalOwner = FakeEventSource.instances.at(-1)!;

      expect(firstOwner.closed).toBe(true);
      expect(secondOwner.closed).toBe(true);
      expect(screen.getByTestId("connected")).toHaveTextContent("false");
      expect(screen.getByTestId("hub-status")).toHaveTextContent("none");

      act(() => {
        for (const stale of FakeEventSource.instances.filter(
          (source) => source !== finalOwner,
        )) {
          stale.onopen?.(new Event("open"));
          stale.emit("eventbus.status", {
            state: "ok",
            degraded: false,
          });
          stale.emit("resync", { latest_id: 401 });
          stale.emit("org.updated", {
            id: 402,
            type: "org.updated",
          });
          stale.onerror?.(new Event("error"));
        }
        vi.advanceTimersByTime(1500);
      });
      expect(FakeEventSource.instances.at(-1)).toBe(finalOwner);
      expect(screen.getByTestId("connected")).toHaveTextContent("false");
      expect(screen.getByTestId("hub-status")).toHaveTextContent("none");
      expect(screen.getByTestId("last-event")).toHaveTextContent("none");

      act(() => {
        finalOwner.onopen?.(new Event("open"));
        finalOwner.emit("eventbus.status", {
          state: "degraded",
          degraded: true,
        });
      });
      expect(screen.getByTestId("connected")).toHaveTextContent("true");
      expect(screen.getByTestId("error")).toHaveTextContent("false");
      expect(screen.getByTestId("hub-status")).toHaveTextContent("degraded");

      const sourceCount = FakeEventSource.instances.length;
      view.unmount();
      expect(finalOwner.closed).toBe(true);
      act(() => {
        finalOwner.onopen?.(new Event("open"));
        finalOwner.emit("eventbus.status", {
          state: "ok",
          degraded: false,
        });
        finalOwner.emit("org.updated", {
          id: 403,
          type: "org.updated",
        });
        finalOwner.onerror?.(new Event("error"));
        vi.advanceTimersByTime(3000);
      });
      expect(FakeEventSource.instances).toHaveLength(sourceCount);
    } finally {
      vi.useRealTimers();
    }
  });
});
