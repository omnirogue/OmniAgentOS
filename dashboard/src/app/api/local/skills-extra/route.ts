import { execFile } from "node:child_process";
import { lstat, readdir, readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { promisify } from "node:util";
import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { deriveDomainCategory } from "@/features/skills/categorize";
import {
  parseClaudeSkillFrontmatter,
  parseRepoSkillDialectA,
  parseRepoSkillPlain,
  sanitizeSkillId,
  truncate,
} from "@/features/skills/parse";
import type {
  DomainSkillEntry,
  DormantSection,
  DormantSeedEntry,
  RepoSkillEntry,
  RepoSkillsSection,
  SkillDetailPayload,
  SkillLibrarySection,
  SkillsExtraPayload,
} from "@/features/skills/types";
import { TOCTOU_SAFE_READ_SCRIPT } from "./toctou-read-script";

/**
 * Local-read data for the /skills page, independent of the backend's
 * `/api/skills` (verified live 2026-08-14: that endpoint serves the OS's
 * internal skill-*selection* registry — DB-backed, mixes ~9 real
 * vault-linked skills with ~33 auto-captured `run-*` quarantine-candidate
 * rows and 6 dormant seed rows — which is NOT the ~/.claude/skills Claude
 * Code library this page is about, despite sharing the "skills" name. See
 * the P3 report for the fuller trace. Three independently try/caught
 * sections; a failure in one must never blank the other two.
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const execFileAsync = promisify(execFile);

const CLAUDE_SKILLS_DIR = "/Users/youruser/.claude/skills";
const REPO_SKILLS_DIR = "/Users/youruser/OmniAgentOS/skills";
const DB_PATH = "/Users/youruser/OmniAgentOS/var/runtime/state.sqlite3";
const SKILL_MD = "SKILL.md";

const EXEC_TIMEOUT_MS = 5_000;
// Deliberately shorter than EXEC_TIMEOUT_MS: these run once per skills-list
// entry (in parallel via Promise.all), and a symlink into a TCC-protected
// root (e.g. ~/Desktop) blocks the child until this fires — keep it tight so
// one refused entry never dominates the page's total load time.
const SKILL_TRAVERSAL_TIMEOUT_MS = 3_000;
// Test-only override, same rationale/pattern as the deadline knobs below —
// lets a test expire the healthy-build cache fast instead of waiting a real
// 60s to exercise the "deadline trips with an existing cache" path. `||
// default` would treat "0" (falsy) the same as unset; check definedness
// explicitly instead, matching resolveDeadlineCooldownMs below.
function resolveCacheTtlMs(): number {
  const raw = process.env.SKILLS_LIST_CACHE_TTL_MS;
  if (raw === undefined || raw === "") return 60_000;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : 60_000;
}

const CACHE_TTL_MS = resolveCacheTtlMs();
// OC5 (cross-lineage review round 2, 2026-08-14): a build where any section
// or entry is degraded gets a much shorter TTL — a transient TCC stall used
// to freeze a scary "unreadable" view for a full minute with no way for the
// user's Retry to repair it (the client's no-store fetch bypasses the browser
// cache but not this module-level one). A genuinely refused (policy, not
// transient) entry does NOT shorten the TTL — see isDegradedPayload below.
const DEGRADED_CACHE_TTL_MS = 5_000;
const DESCRIPTION_TRUNCATE = 200;
const MAX_BUFFER = 1024 * 1024;
// OC1 (round 2): overridable for tests only — Node's execFile `timeout`
// sends SIGTERM once and never escalates, so a child that ignores it (or a
// kernel wait it can't be signalled out of) leaves the promise pending
// forever; the watchdog below is the only thing that bounds a GET when that
// happens, and needs a short deadline to be testable without a real ~15s
// sleep. OC9 (round 3, 2026-08-14): the override was accepted UNVALIDATED —
// "-1"/"1" are both numerically truthy, so setTimeout clamped them to ~1ms
// and every list request permanently 503'd on a perfectly healthy box.
// Clamp to a sane range and warn once so a bad override is loud, not silent.
const BUILD_DEADLINE_DEFAULT_MS = 15_000;
const BUILD_DEADLINE_MIN_MS = 1_000;
const BUILD_DEADLINE_MAX_MS = 120_000;

function resolveBuildDeadlineMs(): number {
  const raw = process.env.SKILLS_LIST_BUILD_DEADLINE_MS;
  if (raw === undefined || raw === "") return BUILD_DEADLINE_DEFAULT_MS;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed)) return BUILD_DEADLINE_DEFAULT_MS;
  const clamped = Math.min(BUILD_DEADLINE_MAX_MS, Math.max(BUILD_DEADLINE_MIN_MS, parsed));
  console.warn(
    `[skills-extra] SKILLS_LIST_BUILD_DEADLINE_MS override applied: requested ${raw}ms, using ${clamped}ms (clamped to [${BUILD_DEADLINE_MIN_MS},${BUILD_DEADLINE_MAX_MS}])`,
  );
  return clamped;
}

const BUILD_DEADLINE_MS = resolveBuildDeadlineMs();

// launchd starts Next with a thin PATH; sqlite3 ships at /usr/bin but match
// the other local routes' PATH patch for consistency.
const EXEC_ENV = {
  ...process.env,
  PATH: `${process.env.PATH ?? ""}:/opt/homebrew/bin:/usr/local/bin:/usr/bin`,
};

function describeError(reason: unknown, cmd?: string, timeoutMs: number = EXEC_TIMEOUT_MS): string {
  if (reason && typeof reason === "object") {
    const err = reason as NodeJS.ErrnoException & { killed?: boolean };
    if (err.code === "ENOENT") return `${cmd ?? "path"} not found`;
    if (err.killed) return `${cmd ?? "command"} timed out after ${timeoutMs / 1000}s`;
  }
  // Opus polish (cross-lineage review round 2, 2026-08-14): a raw exec
  // error's `.message` embeds the full command line and stderr — useful for
  // debugging, not something to hand a client. Log it server-side instead
  // and return a human-safe summary.
  console.error(`[skills-extra] ${cmd ?? "operation"} failed:`, reason);
  return `${cmd ?? "operation"} failed`;
}

/**
 * TCC-safe traversal for the ~/.claude/skills tree. `scraper` is a symlink
 * into ~/Desktop, which is macOS TCC-protected; the launchd-started Next
 * server has no Desktop consent, so an IN-PROCESS fs call that TRAVERSES the
 * link (fs/promises `realpath()`/`readFile()`) blocks FOREVER inside the
 * tccd consult. That block never resolves on its own, and it permanently
 * pins one of the process's libuv threadpool workers (default pool size 4)
 * — a couple of hits wedge every other fs call in the app. A child process
 * doing the same traversal is KILLABLE on timeout (SIGTERM frees everything
 * the child held), so every traversal below goes through execFileAsync
 * instead of the in-process fs/promises calls. `lstat()` on the symlink
 * itself stays in-process — nofollow, it never traverses the target.
 *
 * MAJOR 3 (opus OC2, cross-lineage review round 2, 2026-08-14): SIGTERM is
 * NOT a hard deadline — Node sends it once and never escalates. A child that
 * traps/ignores it (or is stuck in an uninterruptible kernel wait) overruns
 * the timeout and can resolve as success with truncated output. Both spawns
 * below pass killSignal: "SIGKILL" (unblockable) so the timeout is a real
 * deadline. BLOCKER 2's watchdog below covers the residual case where even
 * SIGKILL can't reap the child fast enough (unlikely, but see BUILD_DEADLINE_MS).
 */

/** A killed child means our timeout fired; anything else is a real exec/read
 *  error. Shared by childRealpath's and childRead's callers so a timeout and
 *  a hard failure get consistently distinguishable outcomes. */
function readFailureReason(reason: unknown): "timeout" | "error" {
  const err = reason as NodeJS.ErrnoException & { killed?: boolean };
  return err?.killed ? "timeout" : "error";
}

/**
 * P1-followup (F3, cross-lineage review round 1, 2026-08-14): a bare `null`
 * cannot distinguish "resolved, but outside the allowlist" from "never
 * resolved at all" — collapsing them silently erased failure evidence (a
 * timed-out lookup on a PLAIN, non-symlinked entry rendered as a healthy
 * skill with just an empty description). Callers must branch on `ok` and,
 * on failure, on `reason` — never coerce this to a boolean/null.
 */
type RealpathResult = { ok: true; path: string } | { ok: false; reason: "timeout" | "error" };

async function childRealpath(path: string): Promise<RealpathResult> {
  try {
    const { stdout } = await execFileAsync("/usr/bin/readlink", ["-fn", path], {
      timeout: SKILL_TRAVERSAL_TIMEOUT_MS,
      encoding: "utf8",
      maxBuffer: MAX_BUFFER,
      env: EXEC_ENV,
      killSignal: "SIGKILL",
    });
    // BLOCKER 1 (F-TRIM-BYPASS, round 2) + F-NEWLINE-STRIP (round 3,
    // 2026-08-14): `-n` suppresses readlink's own trailing newline for an
    // EXISTING path (verified on this box's BSD readlink, macOS 26.6, with a
    // fixture whose filename itself ends in a literal "\n"). Do NOT
    // `.trim()`/`.trimEnd()` — and, per round 3, do NOT even the earlier
    // "defensive" single-newline strip either: that ALSO collapsed a
    // filename whose real, significant final byte is "\n" onto a sibling
    // entry that could symlink outside the allowlist or simply be the wrong
    // file. `-fn` is trusted fully: any "\n" in its output is part of the
    // real, resolved filename, never a line terminator to remove.
    return { ok: true, path: stdout };
  } catch (reason) {
    return { ok: false, reason: readFailureReason(reason) };
  }
}

// SHOULD 6 (grok F-TOCTOU-SWAP, round 2) + F-TOCTOU-PARENT/F-FIFO-BLOCK
// (grok, round 3, 2026-08-14): `cat` follows symlinks. Between the allowlist
// gate resolving a path and this read, that exact path could be replaced —
// `cat` would happily follow it and serve whatever it now points to. This
// tiny Node child:
//   1. opens O_NOFOLLOW (refuses a symlink swapped into the FINAL path
//      component) | O_NONBLOCK (a FIFO swapped in would otherwise block the
//      open() itself until a writer connects — up to the 3s SIGKILL — this
//      makes a FIFO's read-side open() return immediately instead; the
//      isFile() check below then refuses it, in ~30ms, verified locally).
//   2. fstat-checks isFile() (refuses a directory/device/fifo/socket).
//   3. Best-effort TOCTOU narrowing (grok F-TOCTOU-PARENT): captures the
//      just-opened fd's {dev,ino}, then re-lstats the SAME path back-to-back
//      and refuses if it now reports a symlink or a DIFFERENT {dev,ino} — an
//      already-open fd keeps referring to its original inode regardless of
//      later path changes, so a parent-directory swap DURING this child's
//      own run (between its open() and this lstat()) resolves the fresh
//      lstat to a different inode and gets caught.
//   4. only then reads the fd.
//
// RESIDUAL, ACCEPTED (coordinator ruling 2026-08-14): this narrows the
// exploit window from inter-process (~50ms, the gap between the separate
// readlink-gate child and this read child) down to two adjacent syscalls
// inside a single child process — it does NOT close a swap that already
// fully happened before this child even started (verified with grok's exact
// fixture: both syscalls then observe the same already-swapped state, so no
// inconsistency is detected). A kernel-atomic close needs openat-per-component
// or fcntl(F_GETPATH), neither reachable from a `node -e` child. Accepted as
// a same-uid, the operator-identity-gated, no-privilege-crossing residual — this
// route only ever runs as the operator's own uid, so a successful attack here already
// requires code execution as the operator, at which point there is no privilege
// boundary left to cross by reading a the operator-owned file directly instead.
// The script itself lives in ./toctou-read-script (a Next route file may not
// carry arbitrary named exports); it is imported here and by the test that
// spawns the exact same source against real fixtures.

async function childRead(path: string): Promise<string> {
  const { stdout } = await execFileAsync(process.execPath, ["-e", TOCTOU_SAFE_READ_SCRIPT, "--", path], {
    timeout: SKILL_TRAVERSAL_TIMEOUT_MS,
    encoding: "utf8",
    maxBuffer: MAX_BUFFER,
    env: EXEC_ENV,
    killSignal: "SIGKILL",
  });
  return stdout;
}

/* ------------------------------------------------------- (a) skill library -- */

async function loadDomainEntry(dirName: string): Promise<DomainSkillEntry | null> {
  const full = join(CLAUDE_SKILLS_DIR, dirName);
  let isSymlink = false;
  try {
    const info = await lstat(full);
    isSymlink = info.isSymbolicLink();
  } catch {
    return null; // vanished between readdir and lstat
  }

  // OC8 (cross-lineage review round 3, 2026-08-14 — favourable-absence
  // reintroduced by round 2's OC6 fix): `readlink -fn` exits 1 IDENTICALLY
  // for a genuinely absent SKILL.md, a dangling symlink, a mode-000
  // directory, and any other stat/IO failure (verified on macOS 26.6) — so
  // treating "exit 1" as "no SKILL.md yet" made an UNREADABLE skill render
  // byte-identical to a healthy one. For a PLAIN (non-symlink) entry, the
  // path `<full>/SKILL.md` is purely LOCAL — `full` is already confirmed not
  // a symlink, and `lstat`'s nofollow semantics never traverse the FINAL
  // component either, so this in-process probe can never land inside a
  // redirected/TCC-protected tree. Probe existence with the REAL errno
  // BEFORE the killable-child gate: ENOENT is the only proof of absence; any
  // other lstat failure (EACCES on a mode-000 dir, etc.) is an instrument
  // failure, not absence. A SYMLINKED entry never takes this branch — its
  // SKILL.md could point anywhere, including a TCC-protected root, so
  // existence can only ever be proven through the killable child below.
  if (!isSymlink) {
    try {
      await lstat(join(full, SKILL_MD));
    } catch (reason) {
      const err = reason as NodeJS.ErrnoException;
      if (err.code === "ENOENT") {
        return {
          id: dirName,
          name: dirName,
          description: "",
          category: deriveDomainCategory(dirName, isSymlink, null),
          isSymlink,
          symlinkTarget: null,
        };
      }
      return {
        id: dirName,
        name: dirName,
        description: "",
        category: gateFailureCategory("error"),
        isSymlink,
        symlinkTarget: null,
      };
    }
    // lstat succeeded — SKILL.md exists in SOME form (a regular file, or
    // even a locally-dangling symlink whose target lstat never follows).
    // Fall through to the normal killable-child gate/read below to actually
    // resolve/read it; if THAT still fails, the existing F3 handling
    // already renders an explicit "unreadable" category — proven existence
    // means a later failure is a real unreadable, never a silent absence.
  }

  // R1/F2: the SAME target-root allowlist the detail path enforces — the
  // list response previously served frontmatter and the realpath of
  // entries the detail path refused (the two halves of one route disagreed
  // about policy). F2 (cross-lineage review round 1, 2026-08-14): this used
  // to be a SECOND readlink spawn on top of a separate childRealpath(full)
  // call for the display target; the gate already resolves dir/SKILL.md,
  // and for a symlinked entry that is (barring SKILL.md itself being an
  // independently symlinked file — vanishingly rare, and arguably the MORE
  // policy-relevant resolution anyway since it's what actually gets read)
  // dir's own target with "/SKILL.md" stripped. One spawn per entry instead
  // of two.
  const gate = await allowedSkillMdPath(full);

  if (!gate.ok) {
    // F3 (cross-lineage review round 1, 2026-08-14): a failed/timed-out
    // resolution must NEVER render as a healthy entry with just an empty
    // description — that erases the failure evidence. "refused" (a real,
    // resolved target outside the allowlist) and "timeout"/"error" (we
    // never learned the target at all) are different facts and get
    // different categories. This applies to symlinked AND (now-proven-to-
    // exist) plain entries alike — the OC6-round shortcut that let a plain
    // entry's gate failure fall back to "healthy" is gone (OC8, above).
    return {
      id: dirName,
      name: dirName,
      description: "",
      category: gateFailureCategory(gate.reason),
      isSymlink,
      symlinkTarget: null, // never leak a target for a gate that didn't pass
    };
  }

  const target = isSymlink ? dirname(gate.path) : null;

  let name = dirName;
  let description = "";
  try {
    const raw = await childRead(gate.path);
    const parsed = parseClaudeSkillFrontmatter(raw);
    if (parsed?.data.name) name = parsed.data.name;
    if (parsed?.data.description) description = parsed.data.description;
  } catch (reason) {
    // F3: same principle for the READ leg — a childRead timeout/error must
    // not be indistinguishable from "this skill just has no description".
    // Opus polish (round 2): the gate already validated `target` before this
    // read was even attempted, so — unlike a gate failure — it's known-good
    // and safe to keep rather than null it out.
    return {
      id: dirName,
      name: dirName,
      description: "",
      category: gateFailureCategory(readFailureReason(reason)),
      isSymlink,
      symlinkTarget: target,
    };
  }

  return {
    id: dirName,
    name,
    description: truncate(description, DESCRIPTION_TRUNCATE),
    category: deriveDomainCategory(dirName, isSymlink, target),
    isSymlink,
    symlinkTarget: target,
  };
}

// Round 3, 2026-08-14: a cold build previously fanned out ALL entries'
// readlink+read child processes at once (unbounded `Promise.all`) — a real
// ~/.claude/skills directory spawns ~117 processes for that, and node -e
// measured ~7.2x the spawn cost of `cat`. Bounds both the OC10 rebuild-storm
// worst case and ordinary cold-start cost. Simple in-house worker-pool
// limiter — no new dependency.
const DOMAIN_ENTRY_CONCURRENCY = 16;

async function mapWithConcurrency<T, R>(items: T[], limit: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let nextIndex = 0;
  async function worker(): Promise<void> {
    for (;;) {
      const index = nextIndex;
      nextIndex += 1;
      if (index >= items.length) return;
      results[index] = await fn(items[index]);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, () => worker()));
  return results;
}

async function loadLibrary(): Promise<SkillLibrarySection> {
  try {
    // OC6 (cross-lineage review round 2, 2026-08-14): readdir used to return
    // bare names with no type info, so a stray FILE (.DS_Store, README.md —
    // anything a Finder visit or a half-authored skill drops in) reached
    // loadDomainEntry, whose gate then ran `readlink -f <file>/SKILL.md` —
    // exits 1 on macOS, rendering a scary "unreadable (error)" card for a
    // file that was never a skill directory at all. Skip anything that
    // isn't a directory or a symlink (matches loadRepoSkills' own pattern).
    const entries = await readdir(CLAUDE_SKILLS_DIR, { withFileTypes: true });
    const names = entries.filter((entry) => entry.isDirectory() || entry.isSymbolicLink()).map((entry) => entry.name);
    const loaded = await mapWithConcurrency(names, DOMAIN_ENTRY_CONCURRENCY, loadDomainEntry);
    const skills = loaded
      .filter((entry): entry is DomainSkillEntry => entry !== null)
      .sort((a, b) => a.category.localeCompare(b.category) || a.name.localeCompare(b.name));
    return { source: "live", error: null, skills, scannedAt: new Date().toISOString() };
  } catch (reason) {
    return { source: "error", error: describeError(reason, CLAUDE_SKILLS_DIR), skills: [], scannedAt: null };
  }
}

/* -------------------------------------------------------- (b) repo skills -- */

async function loadRepoSkillEntry(dirName: string): Promise<RepoSkillEntry | null> {
  const path = join(REPO_SKILLS_DIR, dirName, SKILL_MD);
  let raw: string;
  try {
    raw = await readFile(path, "utf8");
  } catch {
    return null;
  }
  const dialectA = parseRepoSkillDialectA(raw);
  if (dialectA) {
    return {
      id: dirName,
      name: dialectA.title,
      summary: truncate(dialectA.summary, DESCRIPTION_TRUNCATE),
      status: dialectA.status,
      dialect: "frontmatter",
      path,
    };
  }
  const plain = parseRepoSkillPlain(dirName, raw);
  return {
    id: dirName,
    name: plain.name,
    summary: truncate(plain.summary, DESCRIPTION_TRUNCATE),
    status: null,
    dialect: "plain",
    path,
  };
}

async function loadRepoSkills(): Promise<RepoSkillsSection> {
  try {
    const entries = await readdir(REPO_SKILLS_DIR, { withFileTypes: true });
    const dirs = entries.filter((entry) => entry.isDirectory()).map((entry) => entry.name);
    const loaded = await Promise.all(dirs.map(loadRepoSkillEntry));
    const skills = loaded.filter((entry): entry is RepoSkillEntry => entry !== null);
    return { source: "live", error: null, skills };
  } catch (reason) {
    return { source: "error", error: describeError(reason, REPO_SKILLS_DIR), skills: [] };
  }
}

/* ------------------------------------------------------ (c) dormant seeds -- */

/**
 * The `skills` DB table's dormant webinar/infrastructure seed rows —
 * migration-inserted, never wired into any live selection path. Scoped to
 * these two categories (not the whole table): the table also holds ~9 real
 * vault-linked skills and ~33 auto-captured `run-*` quarantine-candidate
 * rows that are neither "the library" nor "dormant seeds" as this page
 * defines them, and presenting them as either would be misleading.
 */
async function loadDormantSeeds(): Promise<DormantSection> {
  const sql =
    "SELECT id, category, status FROM skills WHERE category IN ('Webinars', 'Infrastructure') ORDER BY category, id;";
  try {
    const { stdout } = await execFileAsync("sqlite3", ["-readonly", "-json", DB_PATH, sql], {
      timeout: EXEC_TIMEOUT_MS,
      encoding: "utf8",
      maxBuffer: MAX_BUFFER,
      env: EXEC_ENV,
    });
    const trimmed = stdout.trim();
    const parsed: unknown = trimmed.length > 0 ? JSON.parse(trimmed) : [];
    if (!Array.isArray(parsed)) {
      return { source: "error", error: "sqlite3 did not return a JSON array", seeds: [] };
    }
    const seeds: DormantSeedEntry[] = parsed.flatMap((row) => {
      if (!row || typeof row !== "object") return [];
      const record = row as Record<string, unknown>;
      if (typeof record.id !== "string") return [];
      return [{
        id: record.id,
        category: typeof record.category === "string" ? record.category : "Uncategorized",
        status: typeof record.status === "string" ? record.status : "unknown",
      }];
    });
    return { source: "live", error: null, seeds };
  } catch (reason) {
    return { source: "error", error: describeError(reason, "sqlite3"), seeds: [] };
  }
}

/* ---------------------------------------------------- [id] detail rewire -- */

// MAJOR 4 (opus OC4, cross-lineage review round 2, 2026-08-14): this used to
// resolve SKILL_TARGET_ROOTS via realpathSync() at MODULE LOAD TIME — a
// SYNCHRONOUS, IN-PROCESS traversal of /Users/youruser/Desktop, the exact
// TCC-protected root this whole file exists to never touch in-process, and
// strictly worse than the original bug: being synchronous, it blocks the
// MAIN THREAD (the whole event loop), not just one of the 4 libuv threadpool
// workers, on every cold start / module reload. These three roots are
// static, non-symlinked, known-canonical paths on this single-tenant box
// (verified 2026-08-14) — hardcode them rather than resolve them at all.
// Tradeoff, accepted deliberately (simplicity over the edge case): if a root
// ever DOES become a symlink, entries resolving under its new location
// fail CLOSED (false-refuse) until this literal is updated — never open.
const SKILL_TARGET_ROOTS = [CLAUDE_SKILLS_DIR, "/Users/youruser/Work", "/Users/youruser/Desktop"];

function isAllowedSkillTarget(path: string): boolean {
  return SKILL_TARGET_ROOTS.some((root) => path === root || path.startsWith(`${root}/`));
}

/** MAJOR 5a (cross-lineage review round 2, 2026-08-14): the list path's
 *  category text and the detail path's error text used to be two SEPARATE
 *  hand-maintained mappings from the same `reason` — free to drift apart.
 *  One shared table now backs both, plus the detail path's HTTP status
 *  (opus polish: 404 only for genuinely absent, 403 for a real policy
 *  refusal, 503 for "we don't know" — timeout or error). */
const GATE_FAILURE_TEXT: Record<
  "refused" | "timeout" | "error",
  { category: string; detailError: string; detailStatus: number }
> = {
  refused: {
    category: "out-of-root (refused)",
    detailError: "skill target is outside approved roots",
    detailStatus: 403,
  },
  timeout: {
    category: "unreadable (timed out)",
    detailError: "skill target resolution timed out",
    detailStatus: 503,
  },
  error: {
    category: "unreadable (error)",
    detailError: "skill target resolution failed",
    detailStatus: 503,
  },
};

function gateFailureCategory(reason: "refused" | "timeout" | "error"): string {
  return GATE_FAILURE_TEXT[reason].category;
}

/** F3 (cross-lineage review round 1, 2026-08-14): "refused" (resolved, but
 *  outside every approved root) and "timeout"/"error" (never resolved at
 *  all) are different facts — collapsing both into a bare null erased which
 *  one happened. */
type SkillMdGateResult = { ok: true; path: string } | { ok: false; reason: "refused" | "timeout" | "error" };

/** R1/R2 shared gate: resolve the SKILL.md's realpath and enforce the
 *  target-root allowlist BEFORE any content is read. */
async function allowedSkillMdPath(dir: string): Promise<SkillMdGateResult> {
  const resolved = await childRealpath(join(dir, SKILL_MD));
  if (!resolved.ok) return { ok: false, reason: resolved.reason };
  return isAllowedSkillTarget(resolved.path) ? { ok: true, path: resolved.path } : { ok: false, reason: "refused" };
}

type DetailResult = { payload: SkillDetailPayload; status: number };

async function loadDetail(rawId: string): Promise<DetailResult> {
  const id = sanitizeSkillId(rawId);
  if (!id) {
    return {
      payload: {
        id: rawId,
        exists: false,
        isSymlink: false,
        symlinkTarget: null,
        name: rawId,
        description: "",
        body: "",
        error: "invalid skill id",
      },
      status: 404,
    };
  }

  const dir = join(CLAUDE_SKILLS_DIR, id);
  // The request name must pass the shape allowlist, and the SKILL.md realpath
  // must resolve under an approved target root before its content is returned.

  let isSymlink = false;
  try {
    const info = await lstat(dir);
    isSymlink = info.isSymbolicLink();
  } catch {
    return {
      payload: {
        id,
        exists: false,
        isSymlink: false,
        symlinkTarget: null,
        name: id,
        description: "",
        body: "",
        error: "skill directory not found",
      },
      status: 404,
    };
  }

  // R2: allowlist FIRST, read second — content the policy forbids is never
  // read into the process (also caps reads at approved targets only).
  // F2 (cross-lineage review round 1, 2026-08-14): one readlink spawn, not
  // two — see the matching comment on loadDomainEntry for why deriving the
  // display target from the gate's own resolution is both cheaper and, if
  // anything, more accurate than resolving `dir` separately. `allowedSkillMdPath`
  // never throws (childRealpath catches internally), so it stays outside
  // the read's try/catch below.
  const gate = await allowedSkillMdPath(dir);
  if (!gate.ok) {
    const text = GATE_FAILURE_TEXT[gate.reason];
    return {
      payload: {
        id,
        exists: false,
        isSymlink,
        symlinkTarget: null, // never leak a target for a gate that didn't pass
        name: id,
        description: "",
        body: "",
        error: text.detailError,
      },
      status: text.detailStatus,
    };
  }

  const target = isSymlink ? dirname(gate.path) : null;

  try {
    const raw = await childRead(gate.path);
    const parsed = parseClaudeSkillFrontmatter(raw);
    return {
      payload: {
        id,
        exists: true,
        isSymlink,
        symlinkTarget: target,
        name: parsed?.data.name ?? id,
        description: parsed?.data.description ?? "",
        body: (parsed?.body ?? raw).trim(),
      },
      status: 200,
    };
  } catch (reason) {
    return {
      payload: {
        id,
        exists: false,
        isSymlink,
        // Opus polish (round 2, 2026-08-14): the gate already validated
        // this target before the read was attempted — unlike a gate
        // failure, it's known-good and safe to keep, not null it out.
        symlinkTarget: target,
        name: id,
        description: "",
        body: "",
        error: describeError(reason, SKILL_MD, SKILL_TRAVERSAL_TIMEOUT_MS),
      },
      status: 503,
    };
  }
}

/* ------------------------------------------------------------------ GET -- */

type Cache = { body: SkillsExtraPayload; expiresAt: number };
let cache: Cache | null = null;
// F4 (cross-lineage review round 1, 2026-08-14): N concurrent cold requests
// used to each start their own readdir/readlink/cat/sqlite3 fan-out — 4
// concurrent cold GETs measured 100 spawns against an expected ceiling of
// 25. Coalesce onto a single in-flight build so concurrent callers share one
// result.
let inFlight: Promise<SkillsExtraPayload> | null = null;
// OC13 (round 3, 2026-08-14): a build that LOST its deadline race keeps
// running in the background (promises aren't cancellable) and, on the old
// code, unconditionally overwrote `cache` with its now-stale result even
// after a LATER, fresher build had already succeeded. Each build captures
// its own sequence number at start and only writes `cache` if it is still
// the most recently STARTED build when it finishes.
let buildSequence = 0;
// OC10 (round 3, 2026-08-14): the OC1 watchdog bounds one REQUEST but not
// the underlying RESOURCE — without this, every request against a
// permanently stalled fs restarts a full fan-out (measured: 5 sequential
// GETs -> 5 independent builds, 5 leaked non-settling child spawns) while
// the previous build's children are still un-reaped. A short cooldown after
// a deadline trip makes the backend probed occasionally, not re-fanned-out
// on every request; a spontaneous success elsewhere still short-circuits
// this immediately via the normal cache-hit check above it.
let deadlineCooldownUntil = 0;
// Test-only override, same rationale as BUILD_DEADLINE_MS above —
// deliberately NOT clamped/warned like the deadline override: this one only
// ever shortens a self-imposed cooldown, it can't disable the endpoint the
// way an unclamped deadline could. `|| default` would treat "0" (falsy) the
// same as unset, which is wrong for an override whose whole point can be
// "no cooldown" — check definedness explicitly instead.
function resolveDeadlineCooldownMs(): number {
  const raw = process.env.SKILLS_LIST_DEADLINE_COOLDOWN_MS;
  if (raw === undefined || raw === "") return 10_000;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : 10_000;
}

const DEADLINE_COOLDOWN_MS = resolveDeadlineCooldownMs();

/** OC13: returns the deadline promise AND a canceller, so the caller can
 *  clear/unref the timer once the build it's racing settles (win or lose) —
 *  a bare `setTimeout` with no handle exposed can only ever fire, wasting a
 *  live timer for up to BUILD_DEADLINE_MS after it's no longer needed. */
function timeoutAfter(ms: number): { promise: Promise<never>; cancel: () => void } {
  let timer: ReturnType<typeof setTimeout>;
  const promise = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error(`skills list build exceeded ${ms}ms deadline`)), ms);
    timer.unref?.();
  });
  return { promise, cancel: () => clearTimeout(timer) };
}

/** OC5 (cross-lineage review round 2, 2026-08-14): only a TRANSIENT failure
 *  (timeout/error — might genuinely repair itself on retry) shortens the
 *  cache TTL. A "refused" entry is a stable policy decision, not a glitch —
 *  it will not un-refuse itself in 5s, so it does not get the short TTL. */
function isDegradedPayload(body: SkillsExtraPayload): boolean {
  if (body.library.source === "error" || body.repoSkills.source === "error" || body.dormant.source === "error") {
    return true;
  }
  return body.library.skills.some(
    (skill) => skill.category === "unreadable (timed out)" || skill.category === "unreadable (error)",
  );
}

async function buildPayload(seq: number): Promise<SkillsExtraPayload> {
  const [library, repoSkills, dormant] = await Promise.all([
    loadLibrary(),
    loadRepoSkills(),
    loadDormantSeeds(),
  ]);
  const body: SkillsExtraPayload = { generatedAt: new Date().toISOString(), library, repoSkills, dormant };
  // OC13: don't let a build that's been superseded by a newer one (its own
  // deadline already tripped, and a later request has since started a fresh
  // build) clobber that newer build's cache entry when it finally settles.
  if (seq === buildSequence) {
    const ttl = isDegradedPayload(body) ? DEGRADED_CACHE_TTL_MS : CACHE_TTL_MS;
    cache = { body, expiresAt: Date.now() + ttl };
  }
  return body;
}

/** BLOCKER 2 (opus OC1, cross-lineage review round 2, 2026-08-14): the
 *  `.finally()`-only cleanup only ran once `buildPayload()` SETTLED — but
 *  execFile's `timeout` sends SIGTERM once and never escalates (MAJOR 3
 *  mitigates that for the child itself, but a kernel-level uninterruptible
 *  wait can still outlast it). A build that never settles used to wedge the
 *  endpoint for the life of the process: every later cold GET awaited the
 *  same dead promise forever, even after the filesystem recovered. Racing
 *  the build against a deadline bounds every GET regardless of how the
 *  underlying child behaves; the `.finally()` on the RACE (not on
 *  buildPayload itself) still clears `inFlight` on the timeout path, so the
 *  next request retries with a fresh build. */
function degradedListPayload(message: string): SkillsExtraPayload {
  const now = new Date().toISOString();
  return {
    generatedAt: now,
    library: { source: "error", error: message, skills: [], scannedAt: null },
    repoSkills: { source: "error", error: message, skills: [] },
    dormant: { source: "error", error: message, seeds: [] },
  };
}

export async function GET(request: NextRequest) {
  const idParam = request.nextUrl.searchParams.get("id");
  if (idParam !== null) {
    const { payload, status } = await loadDetail(idParam);
    return NextResponse.json(payload, { status });
  }

  const now = Date.now();
  if (cache && cache.expiresAt > now) return NextResponse.json(cache.body);

  if (now < deadlineCooldownUntil) {
    // OC10: a recent build already blew its deadline — don't restart a full
    // fan-out on every request while the backend is still stalled; serve
    // the same stale/503 fallback until the cooldown elapses.
    console.error("[skills-extra] list build in deadline cooldown, skipping rebuild");
    if (cache) return NextResponse.json({ ...cache.body, stale: true });
    return NextResponse.json(degradedListPayload("skills list build recently timed out; retrying shortly"), {
      status: 503,
    });
  }

  if (!inFlight) {
    const seq = (buildSequence += 1);
    const watchdog = timeoutAfter(BUILD_DEADLINE_MS);
    // OC13: cancel the watchdog once the build itself settles — win or lose,
    // it's no longer needed, and a losing build that finishes later must not
    // leave a stray timer around (it's unref'd regardless, but this avoids
    // depending on that alone).
    const build = buildPayload(seq).finally(() => watchdog.cancel());
    inFlight = Promise.race([build, watchdog.promise]).finally(() => {
      inFlight = null;
    });
  }

  try {
    const body = await inFlight;
    return NextResponse.json(body);
  } catch (reason) {
    console.error("[skills-extra] list build did not settle within the deadline:", reason);
    deadlineCooldownUntil = Date.now() + DEADLINE_COOLDOWN_MS;
    if (cache) {
      // Serve the last known snapshot rather than nothing — explicitly
      // marked stale so the client never mistakes it for a fresh read.
      return NextResponse.json({ ...cache.body, stale: true });
    }
    return NextResponse.json(degradedListPayload("skills list build timed out"), { status: 503 });
  }
}
