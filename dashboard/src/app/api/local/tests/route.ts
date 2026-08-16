import { execFile } from "node:child_process";
import { readdir, readFile, stat } from "node:fs/promises";
import { basename, join } from "node:path";
import { promisify } from "node:util";
import { NextResponse } from "next/server";
import { classifyRun, parsePytestSummary } from "@/features/tests/parse";
import type {
  CiRun,
  CiSection,
  GateRun,
  GateRunError,
  GateRunsSection,
  GateStep,
  LandingsSection,
  TestsPayload,
} from "@/features/tests/types";

/**
 * Local-read observatory for `/tests`. Reads merge-gate run receipts from the
 * serving checkout's evidence dir and shells out to `gh`/`git` (timeouts,
 * no other exec). Failures become explicit `{ error }` sections — never an
 * empty array standing in for "healthy / nothing happened".
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const execFileAsync = promisify(execFile);

const GATE_DIR = "/Users/youruser/OmniAgentOS/var/gate-evidence/records/merge-gate";
const REPO_ROOT = "/Users/youruser/OmniAgentOS";
const GH_REPO = "Globex/OmniAgentOS";

const GATE_TTL_MS = 30_000;
const CI_TTL_MS = 5 * 60_000;
const EXEC_TIMEOUT_MS = 10_000;
const NEWEST_RUNS = 20;
const DETAIL_RAW_CHARS = 120;

const EXEC_ENV = {
  ...process.env,
  PATH: `${process.env.PATH ?? ""}:/opt/homebrew/bin:/usr/local/bin`,
};

type Cache = {
  response: { expiresAt: number; body: TestsPayload } | null;
  ci: { expiresAt: number; body: CiSection } | null;
};

const cache: Cache = { response: null, ci: null };

function describeError(reason: unknown, cmd?: string): string {
  if (reason && typeof reason === "object") {
    const err = reason as NodeJS.ErrnoException & { killed?: boolean };
    if (err.code === "ENOENT") return `${cmd ?? "command"} not on PATH`;
    if (err.killed) return `${cmd ?? "command"} timed out after ${EXEC_TIMEOUT_MS / 1000}s`;
  }
  if (reason instanceof Error) return reason.message;
  return String(reason);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asStringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asExitCode(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const n = Number(value);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

/** Receipts sometimes mint `instrument_error: true` (boolean), not a string. */
function asInstrumentError(value: unknown): string | null {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed.length > 0 ? value : null;
  }
  if (value === true) return "true";
  return null;
}

function durationSeconds(start: string | null, end: string | null): number | null {
  if (!start || !end) return null;
  const a = Date.parse(start);
  const b = Date.parse(end);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null;
  return (b - a) / 1000;
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

function localYmd(d: Date): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

function lastSevenLocalDays(now: Date): string[] {
  const days: string[] = [];
  for (let i = 6; i >= 0; i -= 1) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
    days.push(localYmd(d));
  }
  return days;
}

function mapSteps(raw: unknown): GateStep[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((item) => {
    const step = isRecord(item) ? item : {};
    const detail = asString(step.detail);
    const started = asStringOrNull(step.started_at);
    const finished = asStringOrNull(step.finished_at);
    return {
      name: asString(step.name),
      status: asString(step.status),
      duration_s: durationSeconds(started, finished),
      counts: parsePytestSummary(detail),
      detailRaw: detail.slice(0, DETAIL_RAW_CHARS),
    };
  });
}

function extractRun(raw: unknown): { run: GateRun } | { error: string } {
  if (!isRecord(raw)) return { error: "receipt is not a JSON object" };
  const candidate_sha = asString(raw.candidate_sha);
  const started_at = asStringOrNull(raw.started_at);
  const finished_at = asStringOrNull(raw.finished_at);
  const exit_code = asExitCode(raw.exit_code);
  const instrument_error = asInstrumentError(raw.instrument_error);
  const refusal_reason = asString(raw.refusal_reason);
  const steps = mapSteps(raw.steps);
  const mode = typeof raw.mode === "string" ? raw.mode : null;
  return {
    run: {
      candidate_sha,
      shortSha: candidate_sha.slice(0, 12),
      branch: asString(raw.branch),
      mode,
      exit_code,
      refusal_reason,
      instrument_error,
      started_at,
      finished_at,
      duration_s: durationSeconds(started_at, finished_at),
      steps,
      refusal_class: classifyRun({
        exit_code,
        instrument_error,
        refusal_reason,
        steps: steps.map((s) => ({ name: s.name, status: s.status })),
      }),
    },
  };
}

async function loadGateRuns(): Promise<GateRunsSection> {
  const errors: GateRunError[] = [];
  let names: string[];
  try {
    names = await readdir(GATE_DIR);
  } catch (reason) {
    return {
      runs: [],
      errors: [{ file: basename(GATE_DIR), error: describeError(reason, "readdir") }],
    };
  }

  const runFiles = names.filter(
    (name) => name.includes(".run-") && name.endsWith(".json") && !name.includes(".junit-"),
  );

  const stated: { name: string; mtimeMs: number }[] = [];
  await Promise.all(
    runFiles.map(async (name) => {
      try {
        const info = await stat(join(GATE_DIR, name));
        if (info.isFile()) stated.push({ name, mtimeMs: info.mtimeMs });
      } catch (reason) {
        errors.push({ file: name, error: describeError(reason, "stat") });
      }
    }),
  );
  stated.sort((a, b) => b.mtimeMs - a.mtimeMs);
  const newest = stated.slice(0, NEWEST_RUNS);

  const loaded = await Promise.all(
    newest.map(async ({ name }): Promise<{ run: GateRun } | { file: string; error: string }> => {
      try {
        const text = await readFile(join(GATE_DIR, name), "utf8");
        let parsed: unknown;
        try {
          parsed = JSON.parse(text);
        } catch (reason) {
          return { file: name, error: describeError(reason, "JSON.parse") };
        }
        const extracted = extractRun(parsed);
        if ("error" in extracted) return { file: name, error: extracted.error };
        return extracted;
      } catch (reason) {
        return { file: name, error: describeError(reason, "read") };
      }
    }),
  );

  const runs: GateRun[] = [];
  for (const item of loaded) {
    if ("run" in item) runs.push(item.run);
    else errors.push({ file: item.file, error: item.error });
  }

  return { runs, errors };
}

function mapCiRun(raw: unknown): CiRun {
  const row = isRecord(raw) ? raw : {};
  return {
    status: asString(row.status),
    conclusion: asString(row.conclusion),
    workflowName: asString(row.workflowName),
    createdAt: asString(row.createdAt),
    headBranch: asString(row.headBranch),
    headSha: asString(row.headSha),
  };
}

async function loadCi(now: number): Promise<CiSection> {
  if (cache.ci && cache.ci.expiresAt > now) return cache.ci.body;

  let body: CiSection;
  try {
    const { stdout } = await execFileAsync(
      "gh",
      [
        "run",
        "list",
        "-R",
        GH_REPO,
        "--limit",
        "10",
        "--json",
        "status,conclusion,workflowName,createdAt,headBranch,headSha",
      ],
      { timeout: EXEC_TIMEOUT_MS, encoding: "utf8", maxBuffer: 2 * 1024 * 1024, env: EXEC_ENV },
    );
    let parsed: unknown;
    try {
      parsed = JSON.parse(stdout);
    } catch (reason) {
      body = { error: `gh stdout was not JSON: ${describeError(reason)}` };
      cache.ci = { expiresAt: now + CI_TTL_MS, body };
      return body;
    }
    if (!Array.isArray(parsed)) {
      body = { error: "gh run list did not return a JSON array" };
    } else {
      body = { runs: parsed.map(mapCiRun) };
    }
  } catch (reason) {
    body = { error: describeError(reason, "gh") };
  }

  cache.ci = { expiresAt: now + CI_TTL_MS, body };
  return body;
}

async function loadLandings(): Promise<LandingsSection> {
  try {
    const { stdout } = await execFileAsync(
      "git",
      ["-C", REPO_ROOT, "log", "origin/main", "--since=7 days", "--pretty=%ad", "--date=short"],
      { timeout: EXEC_TIMEOUT_MS, encoding: "utf8", maxBuffer: 2 * 1024 * 1024, env: EXEC_ENV },
    );
    const counts = new Map<string, number>();
    for (const line of stdout.split("\n")) {
      const date = line.trim();
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) continue;
      counts.set(date, (counts.get(date) ?? 0) + 1);
    }
    const now = new Date();
    const days = lastSevenLocalDays(now).map((date) => ({
      date,
      count: counts.get(date) ?? 0,
    }));
    const today = localYmd(now);
    const todayCount = days.find((d) => d.date === today)?.count ?? 0;
    return { days, todayCount };
  } catch (reason) {
    return { error: describeError(reason, "git") };
  }
}

export async function GET() {
  try {
    const now = Date.now();
    if (cache.response && cache.response.expiresAt > now) {
      return NextResponse.json(cache.response.body);
    }

    const [gateRuns, ci, landings] = await Promise.all([
      loadGateRuns(),
      loadCi(now),
      loadLandings(),
    ]);

    const body: TestsPayload = {
      generatedAt: new Date().toISOString(),
      gateRuns,
      ci,
      landings,
    };
    cache.response = { expiresAt: now + GATE_TTL_MS, body };
    return NextResponse.json(body);
  } catch (reason) {
    return NextResponse.json({ error: String(reason) }, { status: 500 });
  }
}
