/**
 * Pins `useComputeEstate`'s poll-error behavior contract (UI-1) and its
 * interval lifecycle (setInterval cleanup on unmount) with a real render —
 * this hook's logic is stateful (`useState`/`useEffect`), so unlike
 * tiles.test.ts's pure-function coverage it needs `renderHook` + a mocked
 * `./client`. Modeled on features/notifications/hooks.test.ts (vi.mock the
 * client module, `renderHook`/`waitFor` from @testing-library/react) and
 * lib/pollWhenVisible.test.ts / lib/useReliabilityEvents.enabled.test.tsx
 * (fake timers + `vi.advanceTimersByTime` for interval/unmount assertions).
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ComputeEstate } from "./types";

const fetchComputeEstateMock = vi.fn();

vi.mock("./client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./client")>();
  return {
    ...actual,
    fetchComputeEstate: () => fetchComputeEstateMock(),
  };
});

import { useComputeEstate } from "./hooks";

function estate(overrides: Partial<ComputeEstate> = {}): ComputeEstate {
  return {
    generated_at: "2026-08-14T00:00:00Z",
    pool: {
      available: true,
      reason: null,
      machines: [],
      depth: { queued: 0, claimed: 0, running: 0, done: 0, parked: 0, cancelled: 0 },
      capacity: { total_cores: 0, total_slots: 0, free_slots: 0, in_flight: 0 },
      refusals_24h: {},
    },
    runners: { available: true, reason: null, runners: [] },
    local: { available: true, reason: null, hostname: "box", load1: 1, load5: 1, ncpu: 4, load_ratio: 0.25 },
    ...overrides,
  };
}

describe("useComputeEstate — poll-error behavior contract (UI-1)", () => {
  afterEach(() => {
    vi.resetAllMocks();
    vi.useRealTimers();
  });

  it("keeps the last-good data and flips `degraded` (status stays ready) when a poll fails AFTER data has landed", async () => {
    fetchComputeEstateMock.mockResolvedValueOnce(estate({ generated_at: "2026-08-14T00:00:00Z" }));
    // pollMs is irrelevant to this test — the second poll is triggered
    // manually via `refresh` (the exact function the interval calls),
    // never by waiting out a real interval tick.
    const { result } = renderHook(() => useComputeEstate(1_000_000));

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.data?.generated_at).toBe("2026-08-14T00:00:00Z");
    expect(result.current.degraded).toBe(false);
    expect(result.current.error).toBeNull();

    fetchComputeEstateMock.mockRejectedValueOnce(new Error("network blip"));
    result.current.refresh();
    await waitFor(() => expect(result.current.degraded).toBe(true));

    // The transient blip is admitted via `degraded`/`error`, but the
    // last-good snapshot and "ready" status are left exactly as they were —
    // never wiped into ErrorState.
    expect(result.current.status).toBe("ready");
    expect(result.current.data?.generated_at).toBe("2026-08-14T00:00:00Z");
    expect(result.current.error).toBe("network blip");
  });

  it("recovers `degraded` back to false on the next successful poll", async () => {
    fetchComputeEstateMock.mockResolvedValueOnce(estate());
    const { result } = renderHook(() => useComputeEstate(1_000_000));
    await waitFor(() => expect(result.current.status).toBe("ready"));

    fetchComputeEstateMock.mockRejectedValueOnce(new Error("network blip"));
    result.current.refresh();
    await waitFor(() => expect(result.current.degraded).toBe(true));

    fetchComputeEstateMock.mockResolvedValueOnce(estate({ generated_at: "2026-08-14T01:00:00Z" }));
    result.current.refresh();
    await waitFor(() => expect(result.current.degraded).toBe(false));

    expect(result.current.status).toBe("ready");
    expect(result.current.error).toBeNull();
    expect(result.current.data?.generated_at).toBe("2026-08-14T01:00:00Z");
  });

  it("goes to status 'error' with data null when a poll fails BEFORE any data has ever landed", async () => {
    fetchComputeEstateMock.mockRejectedValueOnce(new Error("no route"));
    const { result } = renderHook(() => useComputeEstate(1_000_000));

    await waitFor(() => expect(result.current.status).toBe("error"));

    expect(result.current.data).toBeNull();
    expect(result.current.degraded).toBe(false);
    expect(result.current.error).toBe("no route");
  });
});

describe("useComputeEstate — interval lifecycle", () => {
  afterEach(() => {
    vi.resetAllMocks();
    vi.useRealTimers();
  });

  it("clears the interval on unmount — no further fetches after unmount", () => {
    vi.useFakeTimers();
    fetchComputeEstateMock.mockResolvedValue(estate());

    // renderHook flushes the mount effect (and its synchronous `load()`
    // call) via an internal act() before returning — the fetch is invoked
    // immediately, independent of whether its promise has resolved yet.
    const { unmount } = renderHook(() => useComputeEstate(1_000));
    expect(fetchComputeEstateMock).toHaveBeenCalledTimes(1);

    unmount();

    // Several poll cycles' worth of fake time — if the interval survived
    // unmount this would ring up more calls.
    act(() => {
      vi.advanceTimersByTime(10_000);
    });

    expect(fetchComputeEstateMock).toHaveBeenCalledTimes(1);
  });

  it("keeps polling on the given interval while still mounted (sanity check for the unmount test above)", () => {
    vi.useFakeTimers();
    fetchComputeEstateMock.mockResolvedValue(estate());

    renderHook(() => useComputeEstate(1_000));
    expect(fetchComputeEstateMock).toHaveBeenCalledTimes(1);

    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(fetchComputeEstateMock).toHaveBeenCalledTimes(2);

    act(() => {
      vi.advanceTimersByTime(2_000);
    });
    expect(fetchComputeEstateMock).toHaveBeenCalledTimes(4);
  });
});
