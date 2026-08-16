import { describe, expect, it } from "vitest";
import {
  categoriesOf,
  effectiveCadenceSeconds,
  emptyFilterState,
  filterJobs,
  groupByCategory,
  jobMatchesFilter,
  parseScheduleDescriptionSeconds,
  sortJobs,
  summarizeHealth,
} from "./filterSort";
import type { SystemJob } from "@/features/routines/systemJobs";

function job(overrides: Partial<SystemJob>): SystemJob {
  return {
    key: overrides.key ?? "job",
    name: "Job",
    executor: "launchd",
    category: "Backlog",
    label: null,
    purpose: "does a thing",
    source: "scripts/x.sh",
    module: null,
    schedule: { kind: "interval", seconds: 60, description: "every minute" },
    env_overrides: [],
    loaded: true,
    plist_present: true,
    last_exit_status: 0,
    last_run_at: null,
    next_fire_at: null,
    health: "healthy",
    health_reason: "",
    managed_candidate: false,
    candidate_reason: "",
    ...overrides,
  };
}

describe("jobMatchesFilter / filterJobs", () => {
  it("passes everything when no filter is set", () => {
    const filter = emptyFilterState();
    expect(jobMatchesFilter(job({ category: "Comms", health: "failing" }), filter)).toBe(true);
  });

  it("filters by category", () => {
    const filter = emptyFilterState();
    filter.categories.add("Comms");
    expect(jobMatchesFilter(job({ category: "Comms" }), filter)).toBe(true);
    expect(jobMatchesFilter(job({ category: "Backlog" }), filter)).toBe(false);
  });

  it("filters by health", () => {
    const filter = emptyFilterState();
    filter.healths.add("failing");
    expect(jobMatchesFilter(job({ health: "failing" }), filter)).toBe(true);
    expect(jobMatchesFilter(job({ health: "healthy" }), filter)).toBe(false);
  });

  it("combines both axes (AND, not OR)", () => {
    const filter = emptyFilterState();
    filter.categories.add("Comms");
    filter.healths.add("failing");
    expect(jobMatchesFilter(job({ category: "Comms", health: "failing" }), filter)).toBe(true);
    expect(jobMatchesFilter(job({ category: "Comms", health: "healthy" }), filter)).toBe(false);
    expect(jobMatchesFilter(job({ category: "Backlog", health: "failing" }), filter)).toBe(false);
  });

  it("filterJobs applies the predicate over a list", () => {
    const filter = emptyFilterState();
    filter.healths.add("failing");
    const jobs = [job({ key: "a", health: "failing" }), job({ key: "b", health: "healthy" })];
    expect(filterJobs(jobs, filter).map((j) => j.key)).toEqual(["a"]);
  });
});

describe("categoriesOf / groupByCategory", () => {
  it("returns distinct categories, alphabetically sorted", () => {
    const jobs = [job({ category: "Zeta" }), job({ category: "Alpha" }), job({ category: "Zeta" })];
    expect(categoriesOf(jobs)).toEqual(["Alpha", "Zeta"]);
  });

  it("groups jobs under each category in alphabetical category order", () => {
    const jobs = [
      job({ key: "1", category: "Zeta" }),
      job({ key: "2", category: "Alpha" }),
      job({ key: "3", category: "Zeta" }),
    ];
    const grouped = groupByCategory(jobs);
    expect(grouped.map((g) => g.category)).toEqual(["Alpha", "Zeta"]);
    expect(grouped.find((g) => g.category === "Zeta")?.jobs.map((j) => j.key)).toEqual(["1", "3"]);
  });

  it("never emits an empty category bucket", () => {
    const grouped = groupByCategory([]);
    expect(grouped).toEqual([]);
  });
});

describe("sortJobs", () => {
  const jobs = [
    job({ key: "b", name: "Bravo", last_run_at: "2026-08-01T00:00:00Z", health: "healthy", schedule: { kind: "interval", seconds: 300, description: "every 5m" } }),
    job({ key: "a", name: "Alpha", last_run_at: "2026-08-10T00:00:00Z", health: "failing", schedule: { kind: "interval", seconds: 60, description: "every minute" } }),
    job({ key: "c", name: "Charlie", last_run_at: null, health: "unknown", schedule: { kind: "unknown", seconds: null, description: "unknown" } }),
  ];

  it("does not mutate the input array", () => {
    const original = [...jobs];
    sortJobs(jobs, "name");
    expect(jobs).toEqual(original);
  });

  it("sorts by name, case-insensitively", () => {
    expect(sortJobs(jobs, "name").map((j) => j.key)).toEqual(["a", "b", "c"]);
  });

  it("sorts by last run, most recent first, nulls last", () => {
    expect(sortJobs(jobs, "lastRun").map((j) => j.key)).toEqual(["a", "b", "c"]);
  });

  it("sorts by health severity, worst first", () => {
    expect(sortJobs(jobs, "health").map((j) => j.key)).toEqual(["a", "c", "b"]);
  });

  it("sorts by schedule cadence, finest-grained first, unknown cadence last", () => {
    expect(sortJobs(jobs, "schedule").map((j) => j.key)).toEqual(["a", "b", "c"]);
  });
});

describe("parseScheduleDescriptionSeconds", () => {
  it("parses the deterministic describe_schedule() calendar/interval formats", () => {
    expect(parseScheduleDescriptionSeconds("hourly")).toBe(3600);
    expect(parseScheduleDescriptionSeconds("every 2h")).toBe(7200);
    expect(parseScheduleDescriptionSeconds("every minute")).toBe(60);
    expect(parseScheduleDescriptionSeconds("every 15 minutes")).toBe(900);
    expect(parseScheduleDescriptionSeconds("every 30s")).toBe(30);
    expect(parseScheduleDescriptionSeconds("daily 00:30 local")).toBe(86400);
    expect(parseScheduleDescriptionSeconds("twice daily 03:30 + 15:30 local")).toBe(43200);
    expect(parseScheduleDescriptionSeconds("3x daily 06:00 + 12:00 + 18:00 local")).toBeCloseTo(28800);
  });

  it("recognizes the REAL abbreviated-weekday format describe_schedule() emits (_WEEKDAYS = Sun/Mon/…, never full names)", () => {
    expect(parseScheduleDescriptionSeconds("Mon 09:00 local")).toBe(604800);
    expect(parseScheduleDescriptionSeconds("Sun 23:00 local")).toBe(604800);
    // A full weekday name is not a format the backend ever emits — it must
    // not accidentally match either (e.g. via a loose prefix check).
    expect(parseScheduleDescriptionSeconds("Monday 09:00 local")).toBeNull();
  });

  it("parses the deterministic 5-field cron shapes describe_schedule() can carry", () => {
    expect(parseScheduleDescriptionSeconds("cron */5 * * * *")).toBe(300);
    expect(parseScheduleDescriptionSeconds("cron */15 * * * *")).toBe(900);
    expect(parseScheduleDescriptionSeconds("cron 30 * * * *")).toBe(3600);
    expect(parseScheduleDescriptionSeconds("cron 0 3 * * *")).toBe(86400);
    expect(parseScheduleDescriptionSeconds("cron 0 3 * * 1")).toBe(604800);
    // A note suffix (`describe_schedule`'s " — <note>") must not break the
    // cron-field parse.
    expect(parseScheduleDescriptionSeconds("cron */5 * * * * — backlog sweep")).toBe(300);
  });

  it("returns null for cron/window/unknown shapes it cannot deterministically parse — never a guess", () => {
    expect(parseScheduleDescriptionSeconds("cron 0,30 * * * *")).toBeNull();
    expect(parseScheduleDescriptionSeconds("cron */5 * * * 1-5")).toBeNull();
    expect(parseScheduleDescriptionSeconds("observation window")).toBeNull();
    expect(parseScheduleDescriptionSeconds("—")).toBeNull();
  });
});

describe("effectiveCadenceSeconds", () => {
  it("prefers schedule.seconds when present, even over a matching description", () => {
    const j = job({ schedule: { kind: "interval", seconds: 120, description: "every 2 minutes" } });
    expect(effectiveCadenceSeconds(j)).toBe(120);
  });

  it("falls back to parsing the description when seconds is null", () => {
    const j = job({ schedule: { kind: "calendar", seconds: null, description: "daily 00:30 local" } });
    expect(effectiveCadenceSeconds(j)).toBe(86400);
  });

  it("NEVER substitutes next_fire_at distance for cadence — a cron due in 30s is still a 5-minute cadence", () => {
    const j = job({
      schedule: { kind: "cron", seconds: null, description: "cron */5 * * * *" },
      next_fire_at: "2026-08-15T12:00:30Z",
    });
    expect(effectiveCadenceSeconds(j)).toBe(300);
  });

  it("is truly Infinity (sorts last) when nothing is measurable, even with a near next_fire_at", () => {
    const j = job({
      schedule: { kind: "unknown", seconds: null, description: "—" },
      next_fire_at: "2026-08-15T12:00:01Z",
    });
    expect(effectiveCadenceSeconds(j)).toBe(Number.POSITIVE_INFINITY);
  });
});

describe("sortJobs('schedule') with real describe_schedule() text", () => {
  it("orders hourly < daily < weekly, an interval job by raw seconds, and unknown last", () => {
    const NOW = new Date("2026-08-15T12:00:00Z");
    const jobs = [
      job({ key: "weekly", schedule: { kind: "calendar", seconds: null, description: "Mon 09:00 local" } }),
      job({ key: "interval-5m", schedule: { kind: "interval", seconds: 300, description: "every 5 minutes" } }),
      job({ key: "daily", schedule: { kind: "calendar", seconds: null, description: "daily 00:30 local" } }),
      job({ key: "hourly", schedule: { kind: "calendar", seconds: null, description: "hourly" } }),
      job({ key: "unknown", schedule: { kind: "unknown", seconds: null, description: "—" }, next_fire_at: null }),
    ];
    expect(sortJobs(jobs, "schedule", NOW).map((j) => j.key)).toEqual([
      "interval-5m",
      "hourly",
      "daily",
      "weekly",
      "unknown",
    ]);
  });

  it("sorts a cron job by its parsed cadence, not by how soon it's next due", () => {
    const NOW = new Date("2026-08-15T12:00:00Z");
    const jobs = [
      // Due in 30s, but its cadence is every 5 minutes — must sort AFTER the
      // genuinely-more-frequent 2-minute interval job, not before it.
      job({
        key: "cron-5m-due-soon",
        schedule: { kind: "cron", seconds: null, description: "cron */5 * * * *" },
        next_fire_at: "2026-08-15T12:00:30Z",
      }),
      job({
        key: "interval-2m",
        schedule: { kind: "interval", seconds: 120, description: "every 2 minutes" },
      }),
    ];
    expect(sortJobs(jobs, "schedule", NOW).map((j) => j.key)).toEqual([
      "interval-2m",
      "cron-5m-due-soon",
    ]);
  });
});

describe("summarizeHealth", () => {
  it("counts every health bucket, including zero buckets", () => {
    const jobs = [job({ health: "failing" }), job({ health: "failing" }), job({ health: "healthy" })];
    expect(summarizeHealth(jobs)).toEqual({
      healthy: 1,
      stale: 0,
      failing: 2,
      unknown: 0,
      not_loaded: 0,
    });
  });

  it("returns all-zero counts for an empty job list", () => {
    expect(summarizeHealth([])).toEqual({
      healthy: 0,
      stale: 0,
      failing: 0,
      unknown: 0,
      not_loaded: 0,
    });
  });
});
