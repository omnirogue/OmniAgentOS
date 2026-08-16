import { execFile } from "node:child_process";
import { access, readdir } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import { NextResponse } from "next/server";
import { normalizeGithubOrigin } from "@/features/repos/lib";
import type { LocalClone, OwnerRepos, RemoteRepo, ReposPayload, Violation } from "@/features/repos/types";

/** A read-only, local inventory for the dashboard's repository observatory. */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const execFileAsync = promisify(execFile);
const OWNERS = ["example-org", "Globex", "initech"] as const;
const GH_TIMEOUT_MS = 15_000;
const GIT_TIMEOUT_MS = 5_000;
const CACHE_TTL_MS = 10 * 60_000;
const MAX_LOCAL_REPOS = 40;
const MAX_BUFFER = 2 * 1024 * 1024;

// launchd starts Next with a thin PATH, so make Homebrew and system-installed CLIs visible.
const EXEC_ENV = {
  ...process.env,
  PATH: `${process.env.PATH ?? ""}:/opt/homebrew/bin:/usr/local/bin`,
};

type Cache = { body: ReposPayload; expiresAt: number };
let cache: Cache | null = null;

type ScanResult = { paths: string[]; truncated: boolean };

function describeError(reason: unknown, command?: string): string {
  if (reason && typeof reason === "object") {
    const err = reason as NodeJS.ErrnoException & { killed?: boolean };
    if (err.code === "ENOENT") return `${command ?? "command"} not on PATH`;
    if (err.killed) return `${command ?? "command"} timed out`;
  }
  return reason instanceof Error ? reason.message : String(reason);
}

async function hasGitEntry(path: string): Promise<boolean> {
  try {
    await access(join(path, ".git"));
    return true;
  } catch {
    return false;
  }
}

/** Walk only the requested shallow roots; no node_modules or .git traversal. */
async function discoverLocalRepos(): Promise<ScanResult> {
  const home = homedir();
  const roots: Array<{ path: string; maxDepth: number }> = [
    { path: join(home, "Repos"), maxDepth: 2 },
    { path: join(home, "OmniAgentOS"), maxDepth: 1 },
    { path: join(home, "Work/OmniAgentOS/Prototypes"), maxDepth: 1 },
    { path: join(home, "Work/OmniAgentOS/Development/Prototypes"), maxDepth: 1 },
  ];
  const paths: string[] = [];
  let truncated = false;

  async function walk(path: string, depth: number, maxDepth: number): Promise<void> {
    if (await hasGitEntry(path)) {
      if (paths.length >= MAX_LOCAL_REPOS) {
        truncated = true;
        return;
      }
      paths.push(path);
      return;
    }
    if (depth >= maxDepth) return;

    try {
      const entries = await readdir(path, { withFileTypes: true });
      for (const entry of entries) {
        if (entry.name === ".git" || entry.name === "node_modules" || !entry.isDirectory()) continue;
        await walk(join(path, entry.name), depth + 1, maxDepth);
        if (truncated) return;
      }
    } catch {
      return;
    }
  }

  for (const root of roots) {
    await walk(root.path, 0, root.maxDepth);
    if (truncated) break;
  }
  return { paths, truncated };
}

async function loadOwner(owner: string): Promise<OwnerRepos> {
  try {
    const { stdout } = await execFileAsync(
      "gh",
      ["repo", "list", owner, "--limit", "100", "--json", "name,visibility,updatedAt,pushedAt,isArchived"],
      { timeout: GH_TIMEOUT_MS, encoding: "utf8", maxBuffer: MAX_BUFFER, env: EXEC_ENV },
    );
    const parsed: unknown = JSON.parse(stdout);
    if (!Array.isArray(parsed)) return { owner, error: "gh repo list did not return a JSON array" };
    const repos: RemoteRepo[] = parsed.flatMap((value) => {
      if (!value || typeof value !== "object") return [];
      const repo = value as Record<string, unknown>;
      if (typeof repo.name !== "string") return [];
      return [{
        name: repo.name,
        visibility: typeof repo.visibility === "string" ? repo.visibility : "unknown",
        updatedAt: typeof repo.updatedAt === "string" ? repo.updatedAt : "",
        pushedAt: typeof repo.pushedAt === "string" ? repo.pushedAt : null,
        isArchived: repo.isArchived === true,
      }];
    });
    return { owner, repos };
  } catch (reason) {
    return { owner, error: describeError(reason, "gh") };
  }
}

/** F03: distinguishes a genuinely clean `git status --porcelain` (empty
 * stdout, command succeeded) from the command failing outright (ENOENT,
 * timeout, not a git repo, permission denied, ...). The old boolean
 * `string | null` return collapsed both to `null`, which serialized as
 * `dirty: 0` -- a git-status timeout read as "clean" and got cached for
 * CACHE_TTL_MS. `ok: false` now carries the failure reason so the caller can
 * render "status unknown" instead of a false-clean badge. */
type GitStatusResult = { ok: true; output: string } | { ok: false; reason: string };

async function gitOutput(path: string, args: string[]): Promise<string | null> {
  try {
    const { stdout } = await execFileAsync(
      "git",
      ["-C", path, ...args],
      { timeout: GIT_TIMEOUT_MS, encoding: "utf8", maxBuffer: MAX_BUFFER, env: EXEC_ENV },
    );
    return stdout.trim() || null;
  } catch {
    return null;
  }
}

async function gitStatus(path: string): Promise<GitStatusResult> {
  try {
    const { stdout } = await execFileAsync(
      "git",
      ["-C", path, "status", "--porcelain"],
      { timeout: GIT_TIMEOUT_MS, encoding: "utf8", maxBuffer: MAX_BUFFER, env: EXEC_ENV },
    );
    return { ok: true, output: stdout };
  } catch (reason) {
    return { ok: false, reason: describeError(reason, "git status") };
  }
}

async function inspectLocal(path: string, remoteNames: Map<string, string>): Promise<LocalClone> {
  const [origin, status, branch] = await Promise.all([
    gitOutput(path, ["remote", "get-url", "origin"]),
    gitStatus(path),
    gitOutput(path, ["rev-parse", "--abbrev-ref", "HEAD"]),
  ]);
  const originKey = normalizeGithubOrigin(origin);
  return {
    path,
    origin,
    branch: branch === "HEAD" ? null : branch,
    // `null` = "status unknown (git failed)" -- NEVER collapsed into 0/clean.
    dirty: status.ok
      ? status.output.split("\n").filter((line) => line.trim().length > 0).length
      : null,
    dirtyError: status.ok ? null : status.reason,
    matchedRemote: originKey ? remoteNames.get(originKey) ?? null : null,
  };
}

function violationFor(local: LocalClone): Violation | null {
  // A `null` dirty count is a visibility violation in its own right -- we
  // cannot vouch this clone is clean, so it may never fall through to the
  // "no violation" branch alongside a genuinely known-clean 0.
  if (local.dirty === null) {
    return { ...local, reason: "status-unknown" };
  }
  if (local.origin !== null && local.dirty === 0) return null;
  return {
    ...local,
    reason: local.origin === null ? (local.dirty > 0 ? "no-origin+dirty" : "no-origin") : "dirty",
  };
}

async function loadRepos(): Promise<ReposPayload> {
  const owners = await Promise.all(OWNERS.map(loadOwner));
  const remoteNames = new Map<string, string>();
  for (const section of owners) {
    for (const repo of section.repos ?? []) remoteNames.set(`${section.owner}/${repo.name}`.toLowerCase(), `${section.owner}/${repo.name}`);
  }
  const discovered = await discoverLocalRepos();
  const locals = await Promise.all(discovered.paths.map((path) => inspectLocal(path, remoteNames)));
  const violations = locals.flatMap((local) => {
    const violation = violationFor(local);
    return violation ? [violation] : [];
  });
  return { owners, locals, violations, truncated: discovered.truncated, scannedAt: new Date().toISOString() };
}

export async function GET() {
  const now = Date.now();
  if (cache && cache.expiresAt > now) return NextResponse.json(cache.body);
  try {
    const body = await loadRepos();
    cache = { body, expiresAt: now + CACHE_TTL_MS };
    return NextResponse.json(body);
  } catch (reason) {
    return NextResponse.json({ error: describeError(reason) }, { status: 500 });
  }
}
