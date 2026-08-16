import { describe, expect, it } from "vitest";
import {
  formatEventHubState,
  formatIncidentCount,
  formatOverallHealth,
  formatWatchCursorAge,
  formatWatchState,
} from "./display";

describe("reliability display contracts (H-15 / B06)", () => {
  it("renders never run for null/undefined/non-finite watch ages", () => {
    expect(formatWatchCursorAge(null)).toBe("never run");
    expect(formatWatchCursorAge(undefined)).toBe("never run");
    expect(formatWatchCursorAge(Number.NaN)).toBe("never run");
  });

  it("never emits undefineds ago for valid ages", () => {
    expect(formatWatchCursorAge(0)).toBe("just now");
    expect(formatWatchCursorAge(12)).toBe("12s ago");
    expect(formatWatchCursorAge(12)).not.toMatch(/undefined/);
  });

  it("maps healthy to last known good and keeps degraded states", () => {
    expect(formatOverallHealth("healthy")).toBe("last known good");
    expect(formatOverallHealth(null)).toBe("last known good");
    expect(formatOverallHealth("degraded")).toBe("degraded");
    expect(formatOverallHealth("critical")).toBe("critical");
    expect(formatOverallHealth("recovering")).toBe("recovering");
  });

  it("renders unavailable for null incident counts and keeps real zeros", () => {
    expect(formatIncidentCount(null)).toBe("unavailable");
    expect(formatIncidentCount(undefined)).toBe("unavailable");
    expect(formatIncidentCount(Number.NaN)).toBe("unavailable");
    expect(formatIncidentCount(0)).toBe("0");
    expect(formatIncidentCount(3)).toBe("3");
  });

  it("surfaces explicit watch states without inventing healthy heartbeats", () => {
    expect(formatWatchState({ state: "never_run", heartbeat_at: null, cursor_at: null, age_seconds: null, stale_after_seconds: null, error: null })).toBe(
      "never run",
    );
    expect(
      formatWatchState({
        state: "last_known_good",
        heartbeat_at: "2026-07-25T16:29:00Z",
        cursor_at: "2026-07-25T16:20:00Z",
        age_seconds: 60,
        stale_after_seconds: 2700,
        error: "watch_state_unavailable",
      }),
    ).toBe("last known good");
    expect(
      formatWatchState({
        state: "unavailable",
        heartbeat_at: null,
        cursor_at: null,
        age_seconds: null,
        stale_after_seconds: null,
        error: "watch_state_unavailable",
      }),
    ).toBe("unavailable");
    expect(
      formatWatchState({
        state: "current",
        heartbeat_at: "t",
        cursor_at: "t",
        age_seconds: 12,
        stale_after_seconds: 2700,
        error: null,
      }),
    ).toBe("12s ago");
  });

  it("formats event hub degraded vs ok", () => {
    expect(formatEventHubState(null)).toBe("unknown");
    expect(formatEventHubState({ state: "ok", degraded: false })).toBe("ok");
    expect(
      formatEventHubState({
        state: "degraded",
        degraded: true,
        last_error: "store down",
      }),
    ).toBe("degraded — store down");
  });
});
