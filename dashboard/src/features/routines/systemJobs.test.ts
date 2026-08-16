import { describe, expect, it } from "vitest";
import {
  executorLabel,
  formatCountdownTo,
  formatRelativeTime,
  healthSeverity,
  loadedLabel,
  systemJobDotState,
  systemJobHealthLabel,
  systemJobHealthTone,
} from "./systemJobs";

const NOW = new Date("2026-07-28T12:00:00Z");

describe("systemJobHealthTone/Label/DotState", () => {
  it("maps every health state to a semantic tone, never a rainbow", () => {
    expect(systemJobHealthTone("healthy")).toBe("ok");
    expect(systemJobHealthTone("stale")).toBe("warn");
    expect(systemJobHealthTone("failing")).toBe("danger");
    expect(systemJobHealthTone("unknown")).toBe("neutral");
    expect(systemJobHealthTone("not_loaded")).toBe("neutral");
  });

  it("labels states for humans", () => {
    expect(systemJobHealthLabel("healthy")).toBe("Healthy");
    expect(systemJobHealthLabel("stale")).toBe("Stale");
    expect(systemJobHealthLabel("failing")).toBe("Failing");
    expect(systemJobHealthLabel("unknown")).toBe("Unknown");
    expect(systemJobHealthLabel("not_loaded")).toBe("Not loaded");
  });

  it("gives unknown/neutral a quiet dot, failing a danger dot", () => {
    expect(systemJobDotState("failing")).toBe("danger");
    expect(systemJobDotState("stale")).toBe("warn");
    expect(systemJobDotState("healthy")).toBe("ok");
    expect(systemJobDotState("not_loaded")).toBe("paused");
  });
});

describe("executorLabel / loadedLabel", () => {
  it("names each executor", () => {
    expect(executorLabel("launchd")).toBe("launchd");
    expect(executorLabel("remote_cron")).toBe("remote cron");
    expect(executorLabel("remote_docker")).toBe("remote docker");
    expect(executorLabel("csi_pipeline")).toBe("CSI pipeline");
    expect(executorLabel("local_cron")).toBe("local cron");
  });

  it("renders an unrecognized executor generically instead of breaking", () => {
    expect(executorLabel("some_new_executor")).toBe("some new executor");
  });

  it("never resolves an inherited Object.prototype member for a hostile/arbitrary key — a Record/object-literal lookup would return a function here, not undefined, so the '??' fallback never fires and React throws", () => {
    // Every one of these is a real Object.prototype member name. A plain
    // object-literal `EXECUTOR_LABELS[executor]` lookup resolves them to
    // inherited functions/values (not `undefined`), so `?? fallback` never
    // fires; the fix is a `Map`, which has no prototype chain to leak.
    for (const hostile of ["__proto__", "constructor", "toString", "hasOwnProperty", "valueOf"]) {
      const label = executorLabel(hostile);
      expect(typeof label).toBe("string");
      expect(label).not.toContain("function");
      expect(label).not.toBe("[object Object]");
    }
    expect(executorLabel("constructor")).toBe("constructor");
    expect(executorLabel("toString")).toBe("toString");
    expect(executorLabel("hasOwnProperty")).toBe("hasOwnProperty");
    expect(executorLabel("__proto__")).toBe("proto");
  });

  it("loaded is tri-state: remote jobs show nothing", () => {
    expect(loadedLabel(true)).toBe("Loaded");
    expect(loadedLabel(false)).toBe("Not loaded");
    expect(loadedLabel(null)).toBeNull();
  });
});

describe("formatRelativeTime", () => {
  it("renders past timestamps as compact ages", () => {
    expect(formatRelativeTime("2026-07-28T11:59:40Z", NOW)).toBe("just now");
    expect(formatRelativeTime("2026-07-28T11:55:00Z", NOW)).toBe("5m ago");
    expect(formatRelativeTime("2026-07-28T10:00:00Z", NOW)).toBe("2h ago");
    expect(formatRelativeTime("2026-07-25T12:00:00Z", NOW)).toBe("3d ago");
  });

  it("is honest about missing or garbage input", () => {
    expect(formatRelativeTime(null, NOW)).toBe("—");
    expect(formatRelativeTime("not-a-date", NOW)).toBe("—");
  });

  it("never renders a negative age for a clock-skewed timestamp", () => {
    expect(formatRelativeTime("2026-07-28T12:00:30Z", NOW)).toBe("just now");
  });
});

describe("formatCountdownTo", () => {
  it("renders future timestamps as compact countdowns", () => {
    expect(formatCountdownTo("2026-07-28T12:04:20Z", NOW)).toBe("in 5m");
    expect(formatCountdownTo("2026-07-28T14:00:00Z", NOW)).toBe("in 2h");
    expect(formatCountdownTo("2026-07-28T23:30:00Z", NOW)).toBe("in 12h");
    expect(formatCountdownTo("2026-07-29T13:00:00Z", NOW)).toBe("in 2d");
  });

  it("a fire time in the past is due now, not negative", () => {
    expect(formatCountdownTo("2026-07-28T11:59:00Z", NOW)).toBe("due now");
  });

  it("is honest about missing or garbage input", () => {
    expect(formatCountdownTo(null, NOW)).toBe("—");
    expect(formatCountdownTo("junk", NOW)).toBe("—");
  });
});

describe("healthSeverity", () => {
  it("sorts worst-first so failures surface", () => {
    const ordered = ["failing", "stale", "unknown", "not_loaded", "healthy"] as const;
    const severities = ordered.map(healthSeverity);
    expect([...severities].sort((a, b) => a - b)).toEqual([...severities]);
    expect(healthSeverity("failing")).toBeLessThan(healthSeverity("healthy"));
  });
});
