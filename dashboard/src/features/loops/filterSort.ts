/**
 * Pure, React-free/network-free helpers for the `/loops` page's filter +
 * sort controls and the derived summary strip. Every branch here is unit
 * testable without a DOM — see filterSort.test.ts.
 */
import { healthSeverity } from "@/lib/format";
import type { SystemJob, SystemJobHealth } from "@/features/routines/systemJobs";

export type LoopsSortKey = "name" | "lastRun" | "health" | "schedule";

export type LoopsFilterState = {
  categories: Set<string>;
  healths: Set<SystemJobHealth>;
};

export const ALL_HEALTHS: SystemJobHealth[] = [
  "failing",
  "stale",
  "unknown",
  "not_loaded",
  "healthy",
];

export function emptyFilterState(): LoopsFilterState {
  return { categories: new Set(), healths: new Set() };
}

/** A job passes when either set is empty (no filter applied on that axis) or
 * the job's value is a member of the corresponding set. */
export function jobMatchesFilter(job: SystemJob, filter: LoopsFilterState): boolean {
  if (filter.categories.size > 0 && !filter.categories.has(job.category)) return false;
  if (filter.healths.size > 0 && !filter.healths.has(job.health)) return false;
  return true;
}

export function filterJobs(jobs: SystemJob[], filter: LoopsFilterState): SystemJob[] {
  return jobs.filter((job) => jobMatchesFilter(job, filter));
}

/** Distinct categories present in `jobs`, sorted alphabetically — feeds the
 * category filter chip list and the grouped-by-category default view. */
export function categoriesOf(jobs: SystemJob[]): string[] {
  return [...new Set(jobs.map((job) => job.category))].sort((a, b) => a.localeCompare(b));
}

/** Groups jobs by category, preserving `categoriesOf`'s alphabetical order.
 * Empty categories never appear (nothing to group). */
export function groupByCategory(jobs: SystemJob[]): Array<{ category: string; jobs: SystemJob[] }> {
  const byCategory = new Map<string, SystemJob[]>();
  for (const job of jobs) {
    const bucket = byCategory.get(job.category);
    if (bucket) bucket.push(job);
    else byCategory.set(job.category, [job]);
  }
  return categoriesOf(jobs).map((category) => ({ category, jobs: byCategory.get(category) ?? [] }));
}

function nameKey(job: SystemJob): string {
  return job.name.toLowerCase();
}

function lastRunKey(job: SystemJob): number {
  if (!job.last_run_at) return -Infinity;
  const parsed = Date.parse(job.last_run_at);
  return Number.isNaN(parsed) ? -Infinity : parsed;
}

const SECOND = 1;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const WEEK = 7 * DAY;

/**
 * `_WEEKDAYS` in `omniagentos/scheduler/system_jobs.py` — three-letter
 * abbreviations, not full names. `describe_schedule()` emits e.g.
 * "Sun 09:00 local", never "Sunday 09:00 local"; matching full names here
 * would silently never match anything the backend actually produces.
 */
const WEEKDAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];

/**
 * A minimal, deterministic 5-field (`minute hour day month weekday`) cron
 * cadence reader — not a general cron parser. Only the handful of shapes
 * that reduce to an unambiguous "fires every N seconds" cadence are
 * recognized; anything else (comma lists, ranges, day-of-month schedules, a
 * step value on a non-minute field, …) returns `null` rather than a guess.
 */
function parseCronCadenceSeconds(cronExpr: string): number | null {
  const fields = cronExpr.trim().split(/\s+/);
  if (fields.length !== 5) return null;
  const [minute, hour, day, month, weekday] = fields as [string, string, string, string, string];
  const isWild = (field: string) => field === "*";
  const isFixed = (field: string) => /^\d+$/.test(field);
  // A SINGLE plain day token — one of 0-7, or a lone 3-letter day name — not
  // "any non-wildcard value". The backend's cron grammar (routines.py:916)
  // also serializes ranges ("1-5"), lists ("1,3,5"), and steps ("*/2")
  // verbatim into this same field; those are ambiguous cadences (could fire
  // 1, 2, or up to 7 days a week) and must fall through to unknown, never be
  // mistaken for the single-day "once a week" case.
  const isDayToken = (field: string) => /^[0-7]$/.test(field) || WEEKDAYS.includes(field);

  // "*/N * * * *" — fires every N minutes.
  const step = /^\*\/(\d+)$/.exec(minute);
  if (step && isWild(hour) && isWild(day) && isWild(month) && isWild(weekday)) {
    return Number(step[1]) * MINUTE;
  }
  // "M * * * *" — a fixed minute, every other field wild: once an hour.
  if (isFixed(minute) && isWild(hour) && isWild(day) && isWild(month) && isWild(weekday)) {
    return HOUR;
  }
  // "M H * * *" — fixed minute+hour, day/month/weekday wild: once a day.
  if (isFixed(minute) && isFixed(hour) && isWild(day) && isWild(month) && isWild(weekday)) {
    return DAY;
  }
  // "M H * * W" — fixed minute+hour+a SINGLE day token, day/month wild: once
  // a week. A range/list/step in the weekday field is ambiguous cadence, not
  // "once a week" — isDayToken rejects those.
  if (isFixed(minute) && isFixed(hour) && isWild(day) && isWild(month) && isDayToken(weekday)) {
    return WEEK;
  }
  return null;
}

/**
 * Parses the machine-generated `describe_schedule()` text
 * (`omniagentos/scheduler/system_jobs.py`) into an exact/approximate cadence
 * in seconds, for the schedules whose `schedule.seconds` is `null`
 * (calendar/cron/window kinds). Only the DETERMINISTIC formats that function
 * emits are matched; a shape it doesn't recognize returns `null` rather than
 * a guess — see `effectiveCadenceSeconds` for what happens next (truly
 * unknown sorts last, never a proxy value).
 */
export function parseScheduleDescriptionSeconds(description: string): number | null {
  const text = description.trim().toLowerCase();
  if (text === "hourly") return HOUR;
  let match = /^every (\d+)h\b/.exec(text);
  if (match) return Number(match[1]) * HOUR;
  if (text === "every minute") return MINUTE;
  match = /^every (\d+) minutes?\b/.exec(text);
  if (match) return Number(match[1]) * MINUTE;
  match = /^every (\d+)s\b/.exec(text);
  if (match) return Number(match[1]) * SECOND;
  if (WEEKDAYS.some((day) => text.startsWith(`${day} `))) return WEEK;
  if (text.startsWith("twice daily")) return DAY / 2;
  match = /^(\d+)x daily\b/.exec(text);
  if (match) return DAY / Number(match[1]);
  if (text.startsWith("daily")) return DAY;
  if (text.startsWith("cron ")) {
    // describe_schedule()'s cron format is `cron <expr>` or
    // `cron <expr> — <note>` — split on the em-dash separator explicitly
    // rather than a single regex, so the note text (which may itself
    // contain digits/spaces) can never be mistaken for cron fields.
    const cronPart = text.slice("cron ".length).split(" — ")[0]!.trim();
    return parseCronCadenceSeconds(cronPart);
  }
  return null;
}

/**
 * Effective cadence in seconds — lower is more frequent, sorts first.
 * Cadence is derived ONLY from the schedule's own definition, never from how
 * soon the next occurrence happens to land: a 5-minute cron due in 30
 * seconds is still a 5-minute cadence, not a 30-second one. Priority:
 * `schedule.seconds` (already numeric, e.g. interval jobs) > a parsed
 * calendar/cron/cadence description ("daily", "hourly", "weekly", "every
 * N …", a 5-field cron expression) > truly unknown sorts last
 * (`Infinity`), never first — an unmeasured cadence is never treated as
 * "runs constantly".
 */
export function effectiveCadenceSeconds(job: SystemJob, _now?: Date): number {
  void _now; // Deliberately unused: cadence never depends on "now" — see above.
  if (job.schedule.seconds !== null) return job.schedule.seconds;
  const parsed = parseScheduleDescriptionSeconds(job.schedule.description);
  return parsed ?? Number.POSITIVE_INFINITY;
}

/** Sorts a COPY of `jobs`; never mutates the input array. Worst-health-first
 * for the "health" key (mirrors `healthSeverity`'s existing convention:
 * lowest number is worst, so ascending numeric order surfaces failures).
 * `now` is accepted for call-site stability (callers already thread a live
 * clock through here for `lastRun`-adjacent UI) but the "schedule" key does
 * not use it — cadence is derived only from the schedule definition, never
 * from how soon the next occurrence happens to land. */
export function sortJobs(jobs: SystemJob[], key: LoopsSortKey, now: Date = new Date()): SystemJob[] {
  const copy = [...jobs];
  switch (key) {
    case "name":
      copy.sort((a, b) => nameKey(a).localeCompare(nameKey(b)));
      break;
    case "lastRun":
      // Most recently run first.
      copy.sort((a, b) => lastRunKey(b) - lastRunKey(a));
      break;
    case "health":
      copy.sort((a, b) => healthSeverity(a.health) - healthSeverity(b.health));
      break;
    case "schedule":
      copy.sort((a, b) => effectiveCadenceSeconds(a, now) - effectiveCadenceSeconds(b, now));
      break;
    default:
      break;
  }
  return copy;
}

export type LoopsSummaryCounts = Record<SystemJobHealth, number>;

/** Derives the summary strip counts CLIENT-SIDE from the job list — the
 * source of truth, per the brief. The snapshot's own `counts` extras
 * (`healthy`/`unknown`/`not_loaded`) are informational only and never read
 * here. */
export function summarizeHealth(jobs: SystemJob[]): LoopsSummaryCounts {
  const counts: LoopsSummaryCounts = {
    healthy: 0,
    stale: 0,
    failing: 0,
    unknown: 0,
    not_loaded: 0,
  };
  for (const job of jobs) {
    counts[job.health] += 1;
  }
  return counts;
}
