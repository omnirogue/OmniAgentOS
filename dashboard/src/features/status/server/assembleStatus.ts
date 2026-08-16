import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import { promisify } from "node:util";
import { parseAlertsTail } from "../parsers/parseAlerts";
import { parseGateTail } from "../parsers/parseGate";
import { parseLastIterationEnd } from "../parsers/parseIteration";
import { summarizeQueue } from "../parsers/parseQueue";
import { lastNonEmptyLine, parseRecyclerLine } from "../parsers/parseRecycler";
import type {
  GateStatus,
  LandingsStatus,
  LoopRoleStatus,
  QueueStatus,
  RecyclerStatus,
  Result,
  StatusResponse,
} from "../types";
import { readTail } from "./fsTail";
import { tmuxHasSession } from "./tmux";

const execFileAsync = promisify(execFile);

/**
 * GROUND TRUTH source paths on this box — hardcoded absolute, not derived
 * from the dashboard's own cwd/worktree, because the daemons this page
 * reports on run against the ONE serving checkout regardless of which
 * worktree happens to be building the dashboard.
 */
const REPO_ROOT = "/Users/youruser/OmniAgentOS";
const GIT_BIN = "/opt/homebrew/bin/git";

const LOOP_ROLES = ["implementer", "reviewer", "planning"] as const;
type LoopRole = (typeof LOOP_ROLES)[number];

const loopLogPath = (role: LoopRole) => `${REPO_ROOT}/var/loopqueue/logs/${role}-loop.log`;
const GATE_LOG_PATH = `${REPO_ROOT}/var/log/gate-loop.log`;
const QUEUE_JSON_PATH = `${REPO_ROOT}/var/loopqueue/state/queue.json`;
const HANG_RECYCLER_LOG_PATH = `${REPO_ROOT}/var/log/hang-recycler.log`;
const ALERTS_MD_PATH = `${REPO_ROOT}/var/loopqueue/ALERTS.md`;

const LOOP_LOG_TAIL_BYTES = 64 * 1024;
const GATE_LOG_TAIL_BYTES = 32 * 1024;
const RECYCLER_LOG_TAIL_BYTES = 8 * 1024;
const ALERTS_TAIL_BYTES = 16 * 1024;

const GIT_TIMEOUT_MS = 5_000;

function describeError(reason: unknown): string {
  if (reason instanceof Error) return reason.message;
  return String(reason);
}

async function loadLoopRole(role: LoopRole): Promise<Result<LoopRoleStatus>> {
  try {
    const [alive, text] = await Promise.all([
      tmuxHasSession(`loop-${role}`),
      readTail(loopLogPath(role), LOOP_LOG_TAIL_BYTES),
    ]);
    const { lastIterEnd, lastRc } = parseLastIterationEnd(text, role);
    return { ok: true, data: { alive, lastIterEnd, lastRc } };
  } catch (reason) {
    return { ok: false, error: describeError(reason) };
  }
}

async function loadGate(): Promise<Result<GateStatus>> {
  try {
    const text = await readTail(GATE_LOG_PATH, GATE_LOG_TAIL_BYTES);
    return { ok: true, data: parseGateTail(text) };
  } catch (reason) {
    return { ok: false, error: describeError(reason) };
  }
}

async function loadQueue(): Promise<Result<QueueStatus>> {
  // F05 ruling (2026-08-14, DASHBOARD-REDO-DESIGN.md amended): the design's
  // original "read top-level keys ONLY -- never parse items[]" rule is
  // superseded. Only top-level keys are ever RETURNED to the client
  // (`summarizeQueue` below), but a full server-side JSON.parse of the whole
  // file is explicitly RULED ACCEPTABLE -- parsing cost was measured trivial
  // at one 1MB file per 15s (this route's own CACHE_TTL_MS in getStatus()
  // below). No code change from the original finding; this comment and the
  // design doc are the fix.
  try {
    const raw = await readFile(QUEUE_JSON_PATH, "utf8");
    const parsed: unknown = JSON.parse(raw);
    return { ok: true, data: summarizeQueue(parsed) };
  } catch (reason) {
    return { ok: false, error: describeError(reason) };
  }
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** UTC midnight of "today" (server clock), for `git log --since`. */
function todayUtcMidnightIso(now: Date): string {
  return `${now.getUTCFullYear()}-${pad2(now.getUTCMonth() + 1)}-${pad2(now.getUTCDate())}T00:00:00Z`;
}

async function loadLandings(): Promise<Result<LandingsStatus>> {
  try {
    const since = todayUtcMidnightIso(new Date());
    const [countResult, lastResult] = await Promise.all([
      execFileAsync(
        GIT_BIN,
        ["-C", REPO_ROOT, "log", "origin/main", `--since=${since}`, "--oneline"],
        { timeout: GIT_TIMEOUT_MS },
      ),
      execFileAsync(
        GIT_BIN,
        ["-C", REPO_ROOT, "log", "origin/main", "-1", "--pretty=%h %ad %s", "--date=format:%H:%MZ"],
        { timeout: GIT_TIMEOUT_MS },
      ),
    ]);
    const countToday = countResult.stdout.split("\n").filter((line) => line.trim().length > 0).length;
    const lastLanding = lastResult.stdout.trim() || null;
    return { ok: true, data: { countToday, lastLanding } };
  } catch (reason) {
    return { ok: false, error: describeError(reason) };
  }
}

async function loadRecycler(): Promise<Result<RecyclerStatus>> {
  try {
    const text = await readTail(HANG_RECYCLER_LOG_PATH, RECYCLER_LOG_TAIL_BYTES);
    const line = lastNonEmptyLine(text);
    return { ok: true, data: parseRecyclerLine(line) };
  } catch (reason) {
    return { ok: false, error: describeError(reason) };
  }
}

async function loadAlerts(): Promise<Result<string[]>> {
  try {
    const text = await readTail(ALERTS_MD_PATH, ALERTS_TAIL_BYTES);
    return { ok: true, data: parseAlertsTail(text) };
  } catch (reason) {
    return { ok: false, error: describeError(reason) };
  }
}

/**
 * Assembles the full status object from ground truth. Every section is
 * fetched and try/caught independently (see the individual `load*`
 * functions) so one dead source (a rotated log, a missing binary) never
 * takes down the sections that are fine.
 */
export async function assembleStatus(): Promise<StatusResponse> {
  const [[implementer, reviewer, planning], gate, queue, landings, recycler, alerts] = await Promise.all([
    Promise.all(LOOP_ROLES.map((role) => loadLoopRole(role))),
    loadGate(),
    loadQueue(),
    loadLandings(),
    loadRecycler(),
    loadAlerts(),
  ]);

  return {
    generatedAt: new Date().toISOString(),
    loops: { implementer, reviewer, planning },
    gate,
    queue,
    landings,
    recycler,
    alerts,
  };
}

const CACHE_TTL_MS = 15_000;

let cache: { data: StatusResponse; expiresAt: number } | null = null;

/**
 * Cached wrapper around `assembleStatus` — a module-level 15s cache so a
 * page with several polling clients (or a fast refresh) doesn't re-exec
 * `git`/`tmux` and re-read six log files on every request.
 */
export async function getStatus(): Promise<StatusResponse> {
  const now = Date.now();
  if (cache && cache.expiresAt > now) return cache.data;
  const data = await assembleStatus();
  cache = { data, expiresAt: now + CACHE_TTL_MS };
  return data;
}
