/**
 * Copied (paths adapted to this repo's alias layout) from the codex-critic
 * repro fixture for round-2 finding f5:
 * .fusion/runs/crit-20260815T113047Z-67699-2724/repro/fixtures/f5-schedule-cadence-sort.test.tsx
 *
 * Covers the two defects that fixture caught: (1) the parser must match the
 * REAL abbreviated-weekday format `describe_schedule()` emits (Sun/Mon/…,
 * never full names), and (2) cadence must come only from the schedule
 * definition, never substitute how-soon-until-next-fire.
 */
import { expect, it } from "vitest";
import {
  effectiveCadenceSeconds,
  parseScheduleDescriptionSeconds,
  sortJobs,
} from "./filterSort";
import type { SystemJob } from "@/features/routines/systemJobs";

const NOW = new Date("2026-08-15T12:00:00Z");

function scheduled(
  key: string,
  kind: SystemJob["schedule"]["kind"],
  description: string,
  nextFireAt: string | null,
): SystemJob {
  return {
    key,
    name: key,
    executor: "launchd",
    category: "Ops",
    label: null,
    purpose: "purpose",
    source: "source",
    module: null,
    schedule: { kind, seconds: null, description },
    env_overrides: [],
    loaded: true,
    plist_present: true,
    last_exit_status: 0,
    last_run_at: null,
    next_fire_at: nextFireAt,
    health: "healthy",
    health_reason: "ok",
    managed_candidate: false,
    candidate_reason: "",
  };
}

it("recognizes the real abbreviated weekday format emitted by describe_schedule()", () => {
  expect(parseScheduleDescriptionSeconds("Sun 09:00 local")).toBe(7 * 24 * 60 * 60);
});

it("sorts by cadence, not by how close the next occurrence happens to be", () => {
  const interval2m: SystemJob = {
    ...scheduled("interval-2m", "interval", "every 2 minutes", null),
    schedule: { kind: "interval", seconds: 120, description: "every 2 minutes" },
  };
  const cron5m = scheduled(
    "cron-5m",
    "cron",
    "cron */5 * * * *",
    "2026-08-15T12:00:30Z",
  );

  expect(effectiveCadenceSeconds(cron5m, NOW)).toBe(300);
  expect(sortJobs([cron5m, interval2m], "schedule", NOW).map((job) => job.key)).toEqual([
    "interval-2m",
    "cron-5m",
  ]);
});
