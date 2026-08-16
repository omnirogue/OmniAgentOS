import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const health = vi.fn();
const pause = vi.fn();
const lastEvent = { current: null as { type: string; ts: string; payload: Record<string, unknown> } | null };

vi.mock("../lib/api", () => ({
  api: {
    health: () => health(),
    pause: () => pause(),
  },
}));

// Poll only while visible: the real helper needs window/document timers we do not want
// firing inside assertions, so it becomes a no-op subscription here.
vi.mock("../lib/pollWhenVisible", () => ({
  startVisibilityPoll: () => () => {},
}));

vi.mock("../lib/useEventChannel", () => ({
  useEventChannel: () => ({ lastEvent: lastEvent.current, events: [], connected: true, error: false }),
}));

import { StatusPill, relativeSince, statusPillView } from "./StatusPill";

const OK_HEALTH = { status: "ok", version: "1", db: true, worker: { alive: true, last_beat_at: null } };

describe("statusPillView", () => {
  it("reports Running/API OK when everything is healthy", () => {
    expect(statusPillView({ health: { ok: true, workerAlive: true }, paused: false, loading: false })).toEqual({
      tone: "ok",
      state: "Running",
      detail: "API OK",
      paused: false,
    });
  });

  it("is amber and Paused when the loop is deliberately held", () => {
    const view = statusPillView({ health: { ok: true, workerAlive: true }, paused: true, loading: false });
    expect(view.tone).toBe("warn");
    expect(view.state).toBe("Paused");
    expect(view.paused).toBe(true);
  });

  it("is amber when the API answers but the worker is not beating", () => {
    const view = statusPillView({ health: { ok: true, workerAlive: false }, paused: false, loading: false });
    expect(view.tone).toBe("warn");
    expect(view.detail).toBe("worker stale");
  });

  it("puts an unreachable API above every other signal", () => {
    // Paused is irrelevant if we cannot trust anything we were told.
    const view = statusPillView({ health: null, paused: true, loading: false });
    expect(view.tone).toBe("danger");
    expect(view.state).toBe("Offline");
  });

  it("is neutral, not alarming, before the first read lands", () => {
    const view = statusPillView({ health: null, paused: null, loading: true });
    expect(view.tone).toBe("neutral");
    expect(view.state).toBe("Checking");
  });
});

describe("relativeSince", () => {
  const now = 1_700_000_000_000;
  it("degrades from just-now to hours", () => {
    expect(relativeSince(now, now)).toBe("synced just now");
    expect(relativeSince(now - 30_000, now)).toBe("synced 30s ago");
    expect(relativeSince(now - 5 * 60_000, now)).toBe("synced 5m ago");
    expect(relativeSince(now - 3 * 3_600_000, now)).toBe("synced 3h ago");
  });

  it("says so when nothing has ever synced", () => {
    expect(relativeSince(null, now)).toBe("never synced");
  });
});

describe("<StatusPill/>", () => {
  beforeEach(() => {
    health.mockReset();
    pause.mockReset();
    lastEvent.current = null;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders the healthy state", async () => {
    health.mockResolvedValue(OK_HEALTH);
    pause.mockResolvedValue({ paused: false });
    const { container } = render(<StatusPill />);
    await waitFor(() => expect(screen.getByText("Running")).toBeTruthy());
    expect(screen.getByText("API OK")).toBeTruthy();
    expect(container.querySelector(".ds-status-pill--ok")).toBeTruthy();
    // The heartbeat only beats on a genuinely healthy system.
    expect(container.querySelector(".ds-status-pill__dot--live")).toBeTruthy();
  });

  it("goes amber and stops the heartbeat when paused", async () => {
    health.mockResolvedValue(OK_HEALTH);
    pause.mockResolvedValue({ paused: true, reason: "Operator pause" });
    const { container } = render(<StatusPill />);
    await waitFor(() => expect(screen.getByText("Paused")).toBeTruthy());
    expect(container.querySelector(".ds-status-pill--warn")).toBeTruthy();
    expect(container.querySelector(".ds-status-pill__dot--live")).toBeNull();
    expect(screen.getByRole("status").getAttribute("title")).toContain("Operator pause");
  });

  it("goes red when the API cannot be reached", async () => {
    health.mockRejectedValue(new Error("network"));
    pause.mockRejectedValue(new Error("network"));
    const { container } = render(<StatusPill />);
    await waitFor(() => expect(screen.getByText("Offline")).toBeTruthy());
    expect(container.querySelector(".ds-status-pill--danger")).toBeTruthy();
    expect(screen.getByText("never synced")).toBeTruthy();
  });

  it("keeps health when only the pause read fails", async () => {
    health.mockResolvedValue(OK_HEALTH);
    pause.mockRejectedValue(new Error("boom"));
    render(<StatusPill />);
    await waitFor(() => expect(screen.getByText("Running")).toBeTruthy());
    expect(screen.getByText("API OK")).toBeTruthy();
  });

  it("flips to Paused the moment a pause.changed frame arrives", async () => {
    health.mockResolvedValue(OK_HEALTH);
    pause.mockResolvedValue({ paused: false });
    const { rerender } = render(<StatusPill />);
    await waitFor(() => expect(screen.getByText("Running")).toBeTruthy());
    lastEvent.current = { type: "pause.changed", ts: "2026-08-04T00:00:00Z", payload: { paused: true } };
    rerender(<StatusPill />);
    await waitFor(() => expect(screen.getByText("Paused")).toBeTruthy());
  });

  it("announces the whole state to assistive tech", async () => {
    health.mockResolvedValue(OK_HEALTH);
    pause.mockResolvedValue({ paused: false });
    render(<StatusPill />);
    await waitFor(() => expect(screen.getByText("Running")).toBeTruthy());
    const pill = screen.getByRole("status");
    expect(pill.getAttribute("aria-live")).toBe("polite");
    expect(pill.getAttribute("aria-label")).toMatch(/System Running, API OK, synced/);
  });
});
