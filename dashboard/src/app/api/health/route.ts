import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { readFile, readdir } from "node:fs/promises";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { promisify } from "node:util";
import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

/**
 * Filesystem bridge for the /health page (capability health map). The
 * `capmap` CLI (tools/capmap/cli.py) is the source of truth for STATUS —
 * this route shells out to it for `status`/`list` rather than
 * reimplementing compute_status/is_stale in TypeScript, per the W7 brief.
 * Everything else this route adds (owner, verification command text,
 * last-good, run history) is plain data assembly from the registry JSON
 * files and the append-only runs.jsonl / state.json the CLI itself writes —
 * never a second status computation.
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const execFileAsync = promisify(execFile);

const EXEC_TIMEOUT_MS = 15_000;
const MAX_BUFFER = 4 * 1024 * 1024;
// Bound how much of runs.jsonl we hold in memory / scan per request. The
// file is append-only and grows slowly (one line per capability per verify
// run); this is generous headroom for "recent history", not an attempt at a
// full archive.
const MAX_RUN_LINES = 20_000;
const RECENT_RUNS_PER_CAPABILITY = 20;

const EXEC_ENV = {
  ...process.env,
  PATH: `${process.env.PATH ?? ""}:/opt/homebrew/bin:/usr/local/bin:/usr/bin`,
};

/** Resolves the repo root that owns tools/capmap, regardless of whether the
 * Next process was started from the repo root or from dashboard/ (both are
 * observed across worktrees/launchd on this estate). Falls back to the
 * parent-of-cwd guess if neither candidate has the CLI, so a misconfigured
 * environment fails at exec time with a readable error rather than here. */
function resolveRepoRoot(): string {
  const candidates = [resolve(process.cwd(), ".."), process.cwd()];
  for (const candidate of candidates) {
    if (existsSync(join(candidate, "tools", "capmap", "cli.py"))) return candidate;
  }
  return candidates[0];
}

const REPO_ROOT = resolveRepoRoot();
const CLI_PATH = join(REPO_ROOT, "tools", "capmap", "cli.py");
const CAPABILITIES_DIR = join(REPO_ROOT, "tools", "capmap", "capabilities");
const STORE_DIR = join(homedir(), "Work", "Ops", "var", "capmap");
const RUNS_PATH = join(STORE_DIR, "runs.jsonl");

const STATUSES = ["OK", "DEGRADED", "DOWN", "CANNOT_EVALUATE", "UNVERIFIED", "STALE"] as const;
export type CapabilityStatus = (typeof STATUSES)[number];

function isStatus(value: unknown): value is CapabilityStatus {
  return typeof value === "string" && (STATUSES as readonly string[]).includes(value);
}

type StatusRow = { company: string; id: string; status: CapabilityStatus; last_verified: string | null };
type ListRow = { company: string; id: string; kind: string; what_it_does: string };
type RegistryEntry = {
  owner: string;
  verification: Record<string, unknown>;
  cadence_seconds: number | null;
  staleness_slo_seconds: number | null;
};
type RunRecord = {
  ts: string;
  capability_id: string;
  status: CapabilityStatus;
  exit_code: number | null;
  latency_ms: number | null;
  evidence: string | null;
  metric: Record<string, unknown> | null;
};

export type CapabilityHealth = {
  id: string;
  name: string;
  company: string;
  kind: string;
  what_it_does: string;
  status: CapabilityStatus;
  last_checked: string | null;
  last_good: string | null;
  metric: Record<string, unknown> | null;
  owner: string;
  evidence: string | null;
};

export type CapabilityDetail = CapabilityHealth & {
  verification_command: string | null;
  recent_runs: RunRecord[];
  last_error: RunRecord | null;
};

export type HealthPayload = { generated_at: string; capabilities: CapabilityHealth[] };

function describeError(reason: unknown, cmd: string): string {
  if (reason && typeof reason === "object") {
    const err = reason as NodeJS.ErrnoException & { killed?: boolean };
    if (err.code === "ENOENT") return `${cmd} not found`;
    if (err.killed) return `${cmd} timed out after ${EXEC_TIMEOUT_MS / 1000}s`;
  }
  console.error(`[api/health] ${cmd} failed:`, reason);
  return `${cmd} failed`;
}

async function runCli<T>(args: string[]): Promise<T> {
  const { stdout } = await execFileAsync("python3", [CLI_PATH, ...args], {
    timeout: EXEC_TIMEOUT_MS,
    encoding: "utf8",
    maxBuffer: MAX_BUFFER,
    env: EXEC_ENV,
    killSignal: "SIGKILL",
  });
  return JSON.parse(stdout) as T;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function loadStatusRows(): Promise<StatusRow[]> {
  const raw = await runCli<unknown[]>(["status", "--json"]);
  return raw.flatMap((row) => {
    if (!isRecord(row) || typeof row.id !== "string" || typeof row.company !== "string" || !isStatus(row.status)) {
      return [];
    }
    return [{
      company: row.company,
      id: row.id,
      status: row.status,
      last_verified: typeof row.last_verified === "string" ? row.last_verified : null,
    }];
  });
}

async function loadListRows(): Promise<ListRow[]> {
  const raw = await runCli<unknown[]>(["list", "--json"]);
  return raw.flatMap((row) => {
    if (!isRecord(row) || typeof row.id !== "string" || typeof row.company !== "string" || typeof row.kind !== "string") {
      return [];
    }
    return [{
      company: row.company,
      id: row.id,
      kind: row.kind,
      what_it_does: typeof row.what_it_does === "string" ? row.what_it_does : "",
    }];
  });
}

/** Reads the registry JSON files directly (read-only) for fields the CLI's
 * `list`/`status` subcommands don't project: owner, and the verification
 * spec used to render a human-readable "how is this checked" string. This
 * is plain data lookup, not a second status computation. */
async function loadRegistryMap(): Promise<Map<string, RegistryEntry>> {
  const map = new Map<string, RegistryEntry>();
  let filenames: string[];
  try {
    filenames = (await readdir(CAPABILITIES_DIR)).filter((name) => name.endsWith(".json"));
  } catch (reason) {
    console.error("[api/health] cannot read capabilities dir:", reason);
    return map;
  }
  await Promise.all(
    filenames.map(async (filename) => {
      try {
        const raw = await readFile(join(CAPABILITIES_DIR, filename), "utf8");
        const parsed: unknown = JSON.parse(raw);
        if (!isRecord(parsed) || typeof parsed.id !== "string") return;
        map.set(parsed.id, {
          owner: typeof parsed.owner === "string" ? parsed.owner : "unassigned",
          verification: isRecord(parsed.verification) ? parsed.verification : {},
          cadence_seconds: typeof parsed.cadence_seconds === "number" ? parsed.cadence_seconds : null,
          staleness_slo_seconds: typeof parsed.staleness_slo_seconds === "number" ? parsed.staleness_slo_seconds : null,
        });
      } catch (reason) {
        console.error(`[api/health] cannot parse ${filename}:`, reason);
      }
    }),
  );
  return map;
}

/** state.json evidence per capability (data lookup only — status itself
 * always comes from the CLI's `status --json`, never recomputed here). */
async function loadEvidenceMap(): Promise<Map<string, string | null>> {
  const map = new Map<string, string | null>();
  try {
    const raw = await readFile(join(STORE_DIR, "state.json"), "utf8");
    const parsed: unknown = JSON.parse(raw);
    if (!isRecord(parsed)) return map;
    for (const [id, value] of Object.entries(parsed)) {
      map.set(id, isRecord(value) && typeof value.evidence === "string" ? value.evidence : null);
    }
  } catch (reason) {
    const err = reason as NodeJS.ErrnoException;
    if (err.code !== "ENOENT") console.error("[api/health] cannot read state.json:", reason);
  }
  return map;
}

function parseRunLine(line: string): RunRecord | null {
  if (!line.trim()) return null;
  try {
    const parsed: unknown = JSON.parse(line);
    if (!isRecord(parsed) || typeof parsed.capability_id !== "string" || typeof parsed.ts !== "string" || !isStatus(parsed.status)) {
      return null;
    }
    return {
      ts: parsed.ts,
      capability_id: parsed.capability_id,
      status: parsed.status,
      exit_code: typeof parsed.exit_code === "number" ? parsed.exit_code : null,
      latency_ms: typeof parsed.latency_ms === "number" ? parsed.latency_ms : null,
      evidence: typeof parsed.evidence === "string" ? parsed.evidence : null,
      metric: isRecord(parsed.metric) ? parsed.metric : null,
    };
  } catch {
    return null;
  }
}

/** All parsed run records, newest last (file append order), capped to
 * MAX_RUN_LINES most recent lines so a very long-lived runs.jsonl doesn't
 * grow this route's memory/latency unbounded. */
async function loadRuns(): Promise<RunRecord[]> {
  let raw: string;
  try {
    raw = await readFile(RUNS_PATH, "utf8");
  } catch (reason) {
    const err = reason as NodeJS.ErrnoException;
    if (err.code !== "ENOENT") console.error("[api/health] cannot read runs.jsonl:", reason);
    return [];
  }
  const lines = raw.split("\n");
  const tail = lines.length > MAX_RUN_LINES ? lines.slice(-MAX_RUN_LINES) : lines;
  return tail.flatMap((line) => {
    const record = parseRunLine(line);
    return record ? [record] : [];
  });
}

function lastGoodByCapability(runs: RunRecord[]): Map<string, string> {
  const map = new Map<string, string>();
  for (const run of runs) {
    if (run.status === "OK") map.set(run.capability_id, run.ts);
  }
  return map;
}

/** A non-verification-type key in a run's metric, if any — the metric bag is
 * a free-form dict the CLI never interprets, so a real business metric (e.g.
 * a future 24h-volume counter) would show up here; today's registry entries
 * carry none, so this legitimately renders null for most rows. */
function extraMetric(metric: Record<string, unknown> | null): Record<string, unknown> | null {
  if (!metric) return null;
  const rest = Object.fromEntries(Object.entries(metric).filter(([key]) => key !== "verification_type"));
  return Object.keys(rest).length > 0 ? rest : null;
}

function describeVerification(spec: Record<string, unknown>): string | null {
  const type = spec.type;
  if (type === "command") {
    if (Array.isArray(spec.argv)) return spec.argv.map(String).join(" ");
    if (typeof spec.command === "string") return spec.command;
    return "command (unset)";
  }
  if (type === "snapshot_field") {
    const source = typeof spec.source === "string" ? spec.source : "?";
    const field = typeof spec.field === "string" ? spec.field : "?";
    return `read ${field} from ${source}`;
  }
  return null;
}

async function buildCapabilities(): Promise<{ capabilities: CapabilityHealth[]; runs: RunRecord[]; registry: Map<string, RegistryEntry> }> {
  const [statusRows, listRows, registry, evidence, runs] = await Promise.all([
    loadStatusRows(),
    loadListRows(),
    loadRegistryMap(),
    loadEvidenceMap(),
    loadRuns(),
  ]);
  const listById = new Map(listRows.map((row) => [row.id, row]));
  const lastGood = lastGoodByCapability(runs);
  const latestRunById = new Map<string, RunRecord>();
  for (const run of runs) latestRunById.set(run.capability_id, run); // last write wins == most recent (append order)

  const capabilities: CapabilityHealth[] = statusRows.map((row) => {
    const meta = listById.get(row.id);
    const reg = registry.get(row.id);
    return {
      id: row.id,
      name: row.id,
      company: row.company,
      kind: meta?.kind ?? "unknown",
      what_it_does: meta?.what_it_does ?? "",
      status: row.status,
      last_checked: row.last_verified,
      last_good: lastGood.get(row.id) ?? null,
      metric: extraMetric(latestRunById.get(row.id)?.metric ?? null),
      owner: reg?.owner ?? "unassigned",
      evidence: evidence.get(row.id) ?? null,
    };
  });

  return { capabilities, runs, registry };
}

export async function GET(request: NextRequest) {
  const id = request.nextUrl.searchParams.get("id");
  try {
    const { capabilities, runs, registry } = await buildCapabilities();

    if (id !== null) {
      const capability = capabilities.find((entry) => entry.id === id);
      if (!capability) {
        return NextResponse.json({ error: `unknown capability id: ${id}` }, { status: 404 });
      }
      const recentRuns = runs
        .filter((run) => run.capability_id === id)
        .slice(-RECENT_RUNS_PER_CAPABILITY)
        .reverse(); // newest first for the drill-in
      const lastError = recentRuns.find((run) => run.status !== "OK") ?? null;
      const reg = registry.get(id);
      const detail: CapabilityDetail = {
        ...capability,
        verification_command: reg ? describeVerification(reg.verification) : null,
        recent_runs: recentRuns,
        last_error: lastError,
      };
      return NextResponse.json(detail);
    }

    const payload: HealthPayload = { generated_at: new Date().toISOString(), capabilities };
    return NextResponse.json(payload);
  } catch (reason) {
    const message = describeError(reason, "capmap");
    return NextResponse.json({ error: message }, { status: 503 });
  }
}
