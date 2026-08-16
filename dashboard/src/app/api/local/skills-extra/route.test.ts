// @vitest-environment node
//
// The project-wide vitest.config.ts default environment is "jsdom". Under
// jsdom, Vitest's node-builtin mocking (below) does not propagate through a
// second import hop — mocking "node:fs/promises" is visible to THIS file's
// own top-level import, but not to route.ts's import of the same specifier
// (verified empirically: identical mock setup, only the environment
// differed). Pure server-route logic has no DOM dependency, so pin this file
// to the "node" environment rather than touch the shared jsdom default.
import type { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Covers the TCC-hang fix (P1) and both cross-lineage review rounds'
 * follow-ups (2026-08-14):
 *
 * Round 1 (GPT-5.6-Sol repros against 87e772898):
 *  - F2: one readlink spawn per list entry, not two (display target derived
 *    from the SKILL.md gate's own resolution).
 *  - F3: a childRealpath/childRead failure must render as an explicit
 *    degraded category, never a healthy-looking empty entry.
 *  - F4: concurrent cold GETs coalesce onto one in-flight build.
 *
 * Round 2 (Grok adversarial + Opus full-lens repros against c68d42e4f):
 *  - BLOCKER 1 (F-TRIM-BYPASS): childRealpath must never `.trim()` its
 *    result — a real filename can end in significant whitespace, and
 *    trimming collapsed the resolved path onto a DIFFERENT sibling entry
 *    that could symlink outside the allowlist.
 *  - BLOCKER 2 (OC1): a never-settling build must not wedge the endpoint
 *    forever — a deadline race bounds every GET and clears the in-flight
 *    slot so the next request retries.
 *  - MAJOR 3 (OC2): killSignal: "SIGKILL" on both exec helpers (SIGTERM is
 *    not a hard deadline).
 *  - MAJOR 4 (OC4): SKILL_TARGET_ROOTS is now hardcoded, no realpathSync.
 *  - MAJOR 5 (OC3): a single GATE_FAILURE_TEXT table backs both the list
 *    category and the detail error/status, and the tests below specifically
 *    target the mutations OC3's mutation-survival probe found: M1 (read-leg
 *    F3 removed), M2 (refused mislabeled), M3 (timeout/error labels
 *    swapped), plus a plain-dir-never-has-symlinkTarget invariant and the
 *    deadline-clears-the-slot retry behaviour.
 *  - SHOULD 6 (F-TOCTOU-SWAP): the read leg no longer uses `cat` (which
 *    follows symlinks) — a tiny Node child opens O_NOFOLLOW + fstat-checks
 *    isFile(), closing the check-then-use race.
 *  - SHOULD 7 (F-TEST-GAP): a fixture-style test asserting the exact raw
 *    (untrimmed) path reaches the read call.
 *  - OC5/OC6: degraded builds get a short cache TTL; readdir now filters to
 *    directories/symlinks only, and a plain dir with no SKILL.md renders as
 *    a normal empty entry rather than "unreadable".
 *
 * These tests mock `execFile` (via its `util.promisify.custom`
 * implementation, matching how Node's real `child_process.execFile`
 * promisifies) so failures resolve deterministically instead of actually
 * sleeping/hanging. `realpathSync` (no longer even called — MAJOR 4),
 * `lstat`, and `readdir` are mocked too, so this stays hermetic to this
 * route's logic and does not depend on any real path existing on the host.
 */

const { execFileImplMock, lstatMock, readdirMock } = vi.hoisted(() => ({
  execFileImplMock: vi.fn(),
  lstatMock: vi.fn(),
  readdirMock: vi.fn(),
}));

vi.mock("node:child_process", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:child_process")>();
  const { promisify: nodePromisify } = await import("node:util");
  const execFile = (() => {
    throw new Error("execFile callback form is not used by this route — only promisify(execFile) is");
  }) as unknown as typeof import("node:child_process").execFile;
  Object.defineProperty(execFile, nodePromisify.custom, {
    value: (...args: unknown[]) => execFileImplMock(...args),
  });
  return { ...actual, execFile };
});

vi.mock("node:fs", async () => {
  const actual = await vi.importActual<typeof import("node:fs")>("node:fs");
  // MAJOR 4: route.ts no longer imports/calls realpathSync at all, but keep
  // this mock so an accidental reintroduction fails a test hermetically
  // rather than touching the real filesystem.
  return { ...actual, realpathSync: (path: string) => path };
});

vi.mock("node:fs/promises", async () => {
  const actual = await vi.importActual<typeof import("node:fs/promises")>("node:fs/promises");
  return { ...actual, lstat: lstatMock, readdir: readdirMock };
});

const CLAUDE_SKILLS_DIR = "/Users/youruser/.claude/skills";
const REPO_SKILLS_DIR = "/Users/youruser/OmniAgentOS/skills";

function makeDetailRequest(id: string): NextRequest {
  const url = new URL(`http://dashboard.test/api/local/skills-extra?id=${id}`);
  const request = new Request(url) as NextRequest;
  Object.defineProperty(request, "nextUrl", { value: url, configurable: true });
  return request;
}

function makeListRequest(): NextRequest {
  const url = new URL("http://dashboard.test/api/local/skills-extra");
  const request = new Request(url) as NextRequest;
  Object.defineProperty(request, "nextUrl", { value: url, configurable: true });
  return request;
}

function timedOut(cmd: string): NodeJS.ErrnoException & { killed: true } {
  return Object.assign(new Error(`${cmd} timed out`), { killed: true as const });
}

function execError(cmd: string): Error & { killed: false; code: number } {
  return Object.assign(new Error(`${cmd} exited non-zero`), { killed: false as const, code: 1 });
}

type FakeDirentKind = "dir" | "symlink" | "file";
function fakeDirent(name: string, kind: FakeDirentKind) {
  return {
    name,
    isDirectory: () => kind === "dir",
    isSymbolicLink: () => kind === "symlink",
    isFile: () => kind === "file",
  };
}

/** `loadRepoSkills` always gets an empty REPO_SKILLS_DIR stub — none of
 *  these tests are about the repo-skills section, and this keeps every test
 *  from having to also account for its own readdir/readFile calls. */
function stubReaddir(claudeSkillEntries: ReturnType<typeof fakeDirent>[]) {
  readdirMock.mockImplementation((dir: string) => {
    if (dir === CLAUDE_SKILLS_DIR) return Promise.resolve(claudeSkillEntries);
    if (dir === REPO_SKILLS_DIR) return Promise.resolve([]);
    return Promise.reject(new Error(`unexpected readdir: ${dir}`));
  });
}

/** Every mocked exec must be for readlink/the-node-read-child/sqlite3 — a
 *  request that reaches ANY unexpected command means a code path ran that
 *  the test didn't intend to exercise. */
function unexpectedExec(file: string) {
  return Promise.reject(new Error(`unexpected exec: ${file}`));
}

/** OC8 (round 3): route.ts now issues TWO distinct lstat calls per plain-dir
 *  entry — one for the directory itself, one for `<dir>/SKILL.md` — and the
 *  fix's whole point is telling those two apart (and telling ENOENT apart
 *  from every other errno on the second one). The blanket
 *  `lstatMock.mockResolvedValue(...)` used elsewhere can't express that;
 *  this path-aware implementation can. */
function stubLstat(byPath: (path: string) => { isSymbolicLink: () => boolean } | "ENOENT" | "EACCES") {
  lstatMock.mockImplementation((path: string) => {
    const result = byPath(path);
    if (result === "ENOENT") return Promise.reject(Object.assign(new Error("ENOENT"), { code: "ENOENT" }));
    if (result === "EACCES") return Promise.reject(Object.assign(new Error("EACCES"), { code: "EACCES" }));
    return Promise.resolve(result);
  });
}

describe("GET /api/local/skills-extra — TCC-safe child reads (P1) + two cross-lineage review rounds", () => {
  let GET: typeof import("./route").GET;

  beforeEach(async () => {
    execFileImplMock.mockReset();
    lstatMock.mockReset();
    readdirMock.mockReset();
    // route.ts holds module-level `cache`/`inFlight` state (F4). Reset the
    // module registry so each test gets a fresh instance instead of
    // inheriting a previous test's cached/in-flight list build.
    vi.resetModules();
    ({ GET } = await import("./route"));
  });

  describe("[id] detail path", () => {
    it("a childRealpath timeout reports 503 with a timeout message, not the refused-policy message (F3/opus-polish)", async () => {
      lstatMock.mockResolvedValue({ isSymbolicLink: () => true });
      execFileImplMock.mockImplementation((file: string) => {
        if (file === "/usr/bin/readlink") return Promise.reject(timedOut("readlink"));
        return unexpectedExec(file);
      });

      const response = await GET(makeDetailRequest("scraper"));
      const body = await response.json();

      expect(response.status).toBe(503);
      expect(body.exists).toBe(false);
      expect(body.isSymlink).toBe(true);
      expect(body.symlinkTarget).toBeNull();
      // A TIMEOUT is not a policy refusal — must not collapse into the
      // "outside approved roots" message a genuinely resolved, disallowed
      // target gets (see the next test).
      expect(body.error).toBe("skill target resolution timed out");
      expect(execFileImplMock).toHaveBeenCalled();
    });

    it("a genuinely resolved but disallowed target reports 403 with the refused-policy message (F3 regression guard)", async () => {
      lstatMock.mockResolvedValue({ isSymbolicLink: () => true });
      execFileImplMock.mockImplementation((file: string) => {
        if (file === "/usr/bin/readlink") {
          return Promise.resolve({ stdout: "/etc/outside-every-approved-root/SKILL.md", stderr: "" });
        }
        return unexpectedExec(file);
      });

      const response = await GET(makeDetailRequest("scraper"));
      const body = await response.json();

      expect(response.status).toBe(403);
      expect(body.exists).toBe(false);
      expect(body.symlinkTarget).toBeNull();
      expect(body.error).toBe("skill target is outside approved roots");
    });

    it("a childRead timeout on an allowed target reports 503, keeping the already-validated symlinkTarget (opus polish)", async () => {
      lstatMock.mockResolvedValue({ isSymbolicLink: () => true });
      execFileImplMock.mockImplementation((file: string, args: string[]) => {
        if (file === "/usr/bin/readlink") return Promise.resolve({ stdout: `${args[1]}`, stderr: "" });
        if (file === process.execPath) return Promise.reject(timedOut("read"));
        return unexpectedExec(file);
      });

      const response = await GET(makeDetailRequest("scraper"));
      const body = await response.json();

      expect(response.status).toBe(503);
      expect(body.exists).toBe(false);
      // Was hardcoded to the (wrong) 5s EXEC_TIMEOUT_MS before this fix wired
      // describeError to the helpers' actual SKILL_TRAVERSAL_TIMEOUT_MS.
      expect(body.error).toBe("SKILL.md timed out after 3s");
      // Opus polish: the gate already validated this target before the read
      // was attempted — unlike a gate failure, it's known-good and kept.
      expect(body.symlinkTarget).toBe(`${CLAUDE_SKILLS_DIR}/scraper`);
    });

    it("resolves normally (200) with a derived target from a single readlink spawn, and the exact node-child read", async () => {
      lstatMock.mockResolvedValue({ isSymbolicLink: () => true });
      const skillMd = "---\nname: Scraper\ndescription: test skill\n---\nbody text";
      let readCallPath: string | null = null;
      execFileImplMock.mockImplementation((file: string, args: string[]) => {
        if (file === "/usr/bin/readlink") return Promise.resolve({ stdout: `${args[1]}`, stderr: "" });
        if (file === process.execPath) {
          readCallPath = args[args.length - 1];
          return Promise.resolve({ stdout: skillMd, stderr: "" });
        }
        return unexpectedExec(file);
      });

      const response = await GET(makeDetailRequest("scraper"));
      const body = await response.json();

      expect(response.status).toBe(200);
      expect(body.exists).toBe(true);
      expect(body.name).toBe("Scraper");
      expect(body.description).toBe("test skill");
      expect(body.body).toBe("body text");
      expect(body.symlinkTarget).toBe(`${CLAUDE_SKILLS_DIR}/scraper`);
      expect(readCallPath).toBe(`${CLAUDE_SKILLS_DIR}/scraper/SKILL.md`);
      // SHOULD 6: the read leg uses a Node child (O_NOFOLLOW+fstat), not `cat`.
      expect(execFileImplMock.mock.calls.some(([file]: unknown[]) => file === "/bin/cat")).toBe(false);
      // F2: only one readlink spawn for this whole request (the gate's own
      // resolution), not a second one for the display target.
      const readlinkCalls = execFileImplMock.mock.calls.filter(([file]: unknown[]) => file === "/usr/bin/readlink");
      expect(readlinkCalls).toHaveLength(1);
    });

    it("a genuinely absent skill directory reports 404", async () => {
      lstatMock.mockRejectedValue(Object.assign(new Error("ENOENT"), { code: "ENOENT" }));
      const response = await GET(makeDetailRequest("nonexistent"));
      const body = await response.json();
      expect(response.status).toBe(404);
      expect(body.error).toBe("skill directory not found");
    });
  });

  describe("list path (loadDomainEntry)", () => {
    it("a childRealpath timeout on a PLAIN (non-symlink) entry yields an explicitly degraded entry, not a healthy-looking one (F3)", async () => {
      stubReaddir([fakeDirent("plain-skill", "dir")]);
      lstatMock.mockResolvedValue({ isSymbolicLink: () => false });
      execFileImplMock.mockImplementation((file: string) => {
        if (file === "/usr/bin/readlink") return Promise.reject(timedOut("readlink"));
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();

      expect(response.status).toBe(200);
      const entry = body.library.skills.find((s: { id: string }) => s.id === "plain-skill");
      expect(entry).toBeDefined();
      expect(entry.category).toBe("unreadable (timed out)");
      expect(entry.description).toBe("");
      expect(entry.isSymlink).toBe(false);
      expect(entry.symlinkTarget).toBeNull();
    });

    it("resolves a symlinked entry with exactly one readlink spawn and the node-child read, not `cat` (F2/SHOULD6)", async () => {
      stubReaddir([fakeDirent("scraper", "symlink")]);
      lstatMock.mockResolvedValue({ isSymbolicLink: () => true });
      const skillMd = "---\nname: Scraper\ndescription: test skill\n---\nbody text";
      execFileImplMock.mockImplementation((file: string, args: string[]) => {
        if (file === "/usr/bin/readlink") return Promise.resolve({ stdout: `${args[1]}`, stderr: "" });
        if (file === process.execPath) return Promise.resolve({ stdout: skillMd, stderr: "" });
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();

      const readlinkCalls = execFileImplMock.mock.calls.filter(([file]: unknown[]) => file === "/usr/bin/readlink");
      expect(readlinkCalls).toHaveLength(1);
      expect(execFileImplMock.mock.calls.some(([file]: unknown[]) => file === "/bin/cat")).toBe(false);

      const entry = body.library.skills.find((s: { id: string }) => s.id === "scraper");
      expect(entry).toBeDefined();
      expect(entry.description).toBe("test skill");
      expect(entry.symlinkTarget).toBe(`${CLAUDE_SKILLS_DIR}/scraper`);
    });

    it("a symlinked entry resolved OUTSIDE every approved root is labeled 'out-of-root (refused)', distinct from an error (MAJOR 5b-1, kills OC3/M2)", async () => {
      stubReaddir([fakeDirent("evil", "symlink")]);
      lstatMock.mockResolvedValue({ isSymbolicLink: () => true });
      execFileImplMock.mockImplementation((file: string) => {
        if (file === "/usr/bin/readlink") {
          return Promise.resolve({ stdout: "/etc/outside-every-approved-root/SKILL.md", stderr: "" });
        }
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();
      const entry = body.library.skills.find((s: { id: string }) => s.id === "evil");
      expect(entry).toBeDefined();
      expect(entry.category).toBe("out-of-root (refused)");
      expect(entry.category).not.toBe("unreadable (error)");
      expect(entry.symlinkTarget).toBeNull();
    });

    it("a symlinked entry whose gate passes but whose READ fails still yields a degraded entry, keeping the validated target (MAJOR 5b-2, kills OC3/M1)", async () => {
      stubReaddir([fakeDirent("scraper", "symlink")]);
      lstatMock.mockResolvedValue({ isSymbolicLink: () => true });
      execFileImplMock.mockImplementation((file: string, args: string[]) => {
        if (file === "/usr/bin/readlink") return Promise.resolve({ stdout: `${args[1]}`, stderr: "" });
        if (file === process.execPath) return Promise.reject(execError("read"));
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();
      const entry = body.library.skills.find((s: { id: string }) => s.id === "scraper");
      expect(entry).toBeDefined();
      // The failure evidence must be an explicit marker, not just an empty
      // description a real, healthy skill (with no description in its own
      // frontmatter) could equally have.
      expect(entry.category).toBe("unreadable (error)");
      expect(entry.description).toBe("");
      expect(entry.symlinkTarget).toBe(`${CLAUDE_SKILLS_DIR}/scraper`);
    });

    it("timeout and error gate failures on symlinked entries get distinct labels (MAJOR 5b-3, kills OC3/M3)", async () => {
      stubReaddir([fakeDirent("timeout-one", "symlink"), fakeDirent("error-one", "symlink")]);
      lstatMock.mockResolvedValue({ isSymbolicLink: () => true });
      execFileImplMock.mockImplementation((file: string, args: string[]) => {
        if (file === "/usr/bin/readlink") {
          const path = args[args.length - 1] as string;
          if (path.includes("timeout-one")) return Promise.reject(timedOut("readlink"));
          if (path.includes("error-one")) return Promise.reject(execError("readlink"));
          return Promise.reject(new Error(`unexpected path: ${path}`));
        }
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();
      const timeoutEntry = body.library.skills.find((s: { id: string }) => s.id === "timeout-one");
      const errorEntry = body.library.skills.find((s: { id: string }) => s.id === "error-one");
      expect(timeoutEntry.category).toBe("unreadable (timed out)");
      expect(errorEntry.category).toBe("unreadable (error)");
      expect(timeoutEntry.category).not.toBe(errorEntry.category);
    });

    it("a plain (non-symlink) directory never carries a symlinkTarget, across healthy/absent/timeout outcomes (MAJOR 5b-4)", async () => {
      stubReaddir([
        fakeDirent("healthy-plain", "dir"),
        fakeDirent("absent-skillmd-plain", "dir"),
        fakeDirent("timeout-plain", "dir"),
      ]);
      // OC8 (round 3): the SKILL.md existence probe is now a SEPARATE lstat
      // call from the directory's own — only "absent-skillmd-plain"'s
      // SKILL.md lstat is ENOENT; the other two exist (and reach the
      // readlink gate, one healthy, one timing out there).
      stubLstat((path) => {
        if (path.endsWith("/SKILL.md")) {
          return path.includes("absent-skillmd-plain") ? "ENOENT" : { isSymbolicLink: () => false };
        }
        return { isSymbolicLink: () => false };
      });
      const skillMd = "---\nname: Healthy\ndescription: ok\n---\nbody";
      execFileImplMock.mockImplementation((file: string, args: string[]) => {
        if (file === "/usr/bin/readlink") {
          const path = args[args.length - 1] as string;
          if (path.includes("healthy-plain")) return Promise.resolve({ stdout: path, stderr: "" });
          if (path.includes("timeout-plain")) return Promise.reject(timedOut("readlink"));
          // absent-skillmd-plain must NEVER reach here — its SKILL.md lstat
          // already proved ENOENT, so the gate is skipped entirely.
          return Promise.reject(new Error(`unexpected path: ${path}`));
        }
        if (file === process.execPath) return Promise.resolve({ stdout: skillMd, stderr: "" });
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();
      for (const id of ["healthy-plain", "absent-skillmd-plain", "timeout-plain"]) {
        const entry = body.library.skills.find((s: { id: string }) => s.id === id);
        expect(entry, id).toBeDefined();
        expect(entry.isSymlink, id).toBe(false);
        expect(entry.symlinkTarget, id).toBeNull();
      }
      // The healthy one still parses; the absent one is NOT "unreadable".
      const absent = body.library.skills.find((s: { id: string }) => s.id === "absent-skillmd-plain");
      expect(absent.category).not.toMatch(/^unreadable/);
      const timeoutEntry = body.library.skills.find((s: { id: string }) => s.id === "timeout-plain");
      expect(timeoutEntry.category).toBe("unreadable (timed out)");
    });

    it("a stray non-directory, non-symlink entry (e.g. .DS_Store) never reaches loadDomainEntry (OC6)", async () => {
      stubReaddir([fakeDirent(".DS_Store", "file")]);
      execFileImplMock.mockImplementation((file: string) => {
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();
      const entry = body.library.skills.find((s: { id: string }) => s.id === ".DS_Store");
      expect(entry).toBeUndefined();
      // The load-bearing assertion: if the readdir filter regressed, this
      // entry would reach loadDomainEntry and call lstat().
      expect(lstatMock).not.toHaveBeenCalled();
    });

    it("a plain directory with no SKILL.md renders as a normal, empty entry — not 'unreadable' (OC6, updated for OC8's lstat probe)", async () => {
      stubReaddir([fakeDirent("half-authored-skill", "dir")]);
      stubLstat((path) => (path.endsWith("/SKILL.md") ? "ENOENT" : { isSymbolicLink: () => false }));
      execFileImplMock.mockImplementation((file: string) => {
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        // The readlink gate must never be reached — ENOENT on the SKILL.md
        // lstat is proof of absence by itself.
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();
      const entry = body.library.skills.find((s: { id: string }) => s.id === "half-authored-skill");
      expect(entry).toBeDefined();
      expect(entry.category).not.toMatch(/^unreadable/);
      expect(entry.category).not.toBe("out-of-root (refused)");
      expect(entry.description).toBe("");
      expect(entry.isSymlink).toBe(false);
    });

    it("F-TRIM-BYPASS regression: a raw resolved path with significant trailing whitespace passes through byte-exact, never generically trimmed (SHOULD 7)", async () => {
      stubReaddir([fakeDirent("scraper", "symlink")]);
      lstatMock.mockResolvedValue({ isSymbolicLink: () => true });
      const rawPathWithTrailingSpace = `${CLAUDE_SKILLS_DIR}/scraper/payload `; // significant trailing space
      const readCalls: string[] = [];
      execFileImplMock.mockImplementation((file: string, args: string[]) => {
        if (file === "/usr/bin/readlink") {
          // -fn genuinely emits no trailing newline for an existing path
          // (verified locally) — the mock reflects that real behavior.
          return Promise.resolve({ stdout: rawPathWithTrailingSpace, stderr: "" });
        }
        if (file === process.execPath) {
          readCalls.push(args[args.length - 1] as string);
          return Promise.resolve({ stdout: "INNOCENT_TRAILING_SPACE\n", stderr: "" });
        }
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();
      const entry = body.library.skills.find((s: { id: string }) => s.id === "scraper");
      expect(entry).toBeDefined();
      // Never trimmed to ".../payload" — the raw, space-terminated bytes are
      // exactly what reached the read call (and, upstream, the allowlist
      // check: gate.ok being true at all proves that leg used the same raw
      // string, since a mistakenly-trimmed sibling path would ALSO pass the
      // allowlist — the read-call assertion is the load-bearing one here,
      // matching what a real sibling-symlink attack would actually swap).
      expect(readCalls).toEqual([rawPathWithTrailingSpace]);
    });

    it("F-NEWLINE-STRIP regression (round 3): a raw resolved path ending in a literal newline is its own byte, never stripped, never collapsed onto a sibling", async () => {
      stubReaddir([fakeDirent("scraper", "symlink")]);
      lstatMock.mockResolvedValue({ isSymbolicLink: () => true });
      // The filename's OWN final byte is "\n" — not a line terminator -fn
      // added, since -fn adds none for an existing path (verified locally
      // with exactly this fixture shape).
      const rawPathEndingInNewline = `${CLAUDE_SKILLS_DIR}/scraper/notes\n`;
      const readCalls: string[] = [];
      execFileImplMock.mockImplementation((file: string, args: string[]) => {
        if (file === "/usr/bin/readlink") return Promise.resolve({ stdout: rawPathEndingInNewline, stderr: "" });
        if (file === process.execPath) {
          readCalls.push(args[args.length - 1] as string);
          return Promise.resolve({ stdout: "NEWLINE_FILE_CONTENT\n", stderr: "" });
        }
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();
      const entry = body.library.skills.find((s: { id: string }) => s.id === "scraper");
      expect(entry).toBeDefined();
      // The OLD "defensive" single-newline strip would have cut this to
      // ".../notes", silently reading a DIFFERENT (sibling) filesystem entry.
      expect(readCalls).toEqual([rawPathEndingInNewline]);
    });
  });

  describe("GET list build coalescing (F4)", () => {
    it("coalesces two concurrent cold GETs into one build", async () => {
      stubReaddir([]);
      execFileImplMock.mockImplementation((file: string) => {
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        return unexpectedExec(file);
      });

      const [r1, r2] = await Promise.all([GET(makeListRequest()), GET(makeListRequest())]);
      const [b1, b2] = await Promise.all([r1.json(), r2.json()]);

      // Same object shared by both waiters, not two independently-built payloads.
      expect(b1.generatedAt).toBe(b2.generatedAt);
      const claudeSkillsReaddirCalls = readdirMock.mock.calls.filter(([dir]: unknown[]) => dir === CLAUDE_SKILLS_DIR);
      expect(claudeSkillsReaddirCalls).toHaveLength(1);
      const sqliteCalls = execFileImplMock.mock.calls.filter(([file]: unknown[]) => file === "sqlite3");
      expect(sqliteCalls).toHaveLength(1);
    });

    it("a deadline-failed build clears the in-flight slot so the next request retries with fresh data (BLOCKER 2, MAJOR 5b-5)", async () => {
      const previousDeadlineEnv = process.env.SKILLS_LIST_BUILD_DEADLINE_MS;
      const previousCooldownEnv = process.env.SKILLS_LIST_DEADLINE_COOLDOWN_MS;
      process.env.SKILLS_LIST_BUILD_DEADLINE_MS = "1000"; // clamp floor (OC9)
      // OC10's cooldown circuit breaker is a SEPARATE mechanism from this
      // test's target (the in-flight slot itself clearing) — zero it out so
      // the second call isn't blocked by the cooldown; OC10 gets its own
      // dedicated test below.
      process.env.SKILLS_LIST_DEADLINE_COOLDOWN_MS = "0";
      vi.resetModules();
      const { GET: freshGET } = await import("./route");
      try {
        stubReaddir([fakeDirent("scraper", "symlink")]);
        lstatMock.mockResolvedValue({ isSymbolicLink: () => true });
        let healthy = false;
        execFileImplMock.mockImplementation((file: string, args: string[]) => {
          if (file === "/usr/bin/readlink") {
            // Phase 1: never settles — SIGTERM/SIGKILL didn't reap it in
            // time, or an uninterruptible kernel wait; the mock bypasses the
            // exec `timeout` option entirely to simulate that residual case.
            if (!healthy) return new Promise(() => {});
            return Promise.resolve({ stdout: `${args[1]}`, stderr: "" });
          }
          if (file === process.execPath) return Promise.resolve({ stdout: "---\nname: S\n---\nbody", stderr: "" });
          if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
          return unexpectedExec(file);
        });

        const first = await freshGET(makeListRequest());
        // No cache exists yet (first-ever request) — the deadline fallback
        // is the explicit 503 degraded payload, never a fake-healthy one.
        expect(first.status).toBe(503);
        const firstBody = await first.json();
        expect(firstBody.library.source).toBe("error");

        healthy = true; // the filesystem recovers
        const second = await freshGET(makeListRequest());
        // FAILS pre-fix: `inFlight` stays the dead promise forever, so this
        // second call would hang (or, with only `.finally()` on
        // buildPayload itself, never even reach a settled state to retry).
        expect(second.status).toBe(200);
        const secondBody = await second.json();
        expect(secondBody.library.skills[0]?.id).toBe("scraper");
      } finally {
        if (previousDeadlineEnv === undefined) delete process.env.SKILLS_LIST_BUILD_DEADLINE_MS;
        else process.env.SKILLS_LIST_BUILD_DEADLINE_MS = previousDeadlineEnv;
        if (previousCooldownEnv === undefined) delete process.env.SKILLS_LIST_DEADLINE_COOLDOWN_MS;
        else process.env.SKILLS_LIST_DEADLINE_COOLDOWN_MS = previousCooldownEnv;
      }
    }, 10_000);

    it("OC10 — 5 sequential GETs against a permanently hung fs start at most 1 build (deadline cooldown circuit breaker)", async () => {
      const previousDeadlineEnv = process.env.SKILLS_LIST_BUILD_DEADLINE_MS;
      process.env.SKILLS_LIST_BUILD_DEADLINE_MS = "60";
      vi.resetModules();
      const { GET: freshGET } = await import("./route");
      try {
        stubReaddir([fakeDirent("scraper", "symlink")]);
        lstatMock.mockResolvedValue({ isSymbolicLink: () => true });
        execFileImplMock.mockImplementation((file: string) => {
          if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
          return new Promise(() => {}); // never settles, even after SIGKILL
        });

        for (let i = 0; i < 5; i += 1) {
          await freshGET(makeListRequest());
          await new Promise((r) => setTimeout(r, 5));
        }
        // FAILS pre-fix (OC10): each of the 5 requests restarted a full
        // build once the previous one's deadline tripped and cleared
        // `inFlight` — 5 independent builds, 5 leaked non-settling spawns.
        const builds = readdirMock.mock.calls.filter(([d]: unknown[]) => d === CLAUDE_SKILLS_DIR).length;
        expect(builds).toBeLessThanOrEqual(1);
      } finally {
        if (previousDeadlineEnv === undefined) delete process.env.SKILLS_LIST_BUILD_DEADLINE_MS;
        else process.env.SKILLS_LIST_BUILD_DEADLINE_MS = previousDeadlineEnv;
      }
    }, 10_000);

    it("a deadline-failed build with an existing cache serves it explicitly marked stale (M8, OC11)", async () => {
      const previousDeadlineEnv = process.env.SKILLS_LIST_BUILD_DEADLINE_MS;
      const previousCooldownEnv = process.env.SKILLS_LIST_DEADLINE_COOLDOWN_MS;
      const previousCacheTtlEnv = process.env.SKILLS_LIST_CACHE_TTL_MS;
      process.env.SKILLS_LIST_BUILD_DEADLINE_MS = "1000";
      process.env.SKILLS_LIST_DEADLINE_COOLDOWN_MS = "0";
      process.env.SKILLS_LIST_CACHE_TTL_MS = "30"; // expire the healthy cache fast
      vi.resetModules();
      const { GET: freshGET } = await import("./route");
      try {
        stubReaddir([fakeDirent("scraper", "symlink")]);
        lstatMock.mockResolvedValue({ isSymbolicLink: () => true });
        let healthy = true;
        execFileImplMock.mockImplementation((file: string, args: string[]) => {
          if (file === "/usr/bin/readlink") {
            if (!healthy) return new Promise(() => {}); // simulates the residual unkillable-child case
            return Promise.resolve({ stdout: args[1], stderr: "" });
          }
          if (file === process.execPath) return Promise.resolve({ stdout: "---\nname: S\n---\nbody", stderr: "" });
          if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
          return unexpectedExec(file);
        });

        const first = await freshGET(makeListRequest());
        expect(first.status).toBe(200);
        const firstBody = await first.json();
        expect(firstBody.stale).toBeUndefined(); // a normal response never carries the marker

        await new Promise((r) => setTimeout(r, 40)); // let the 30ms cache TTL expire
        healthy = false; // the NEXT build hangs

        const second = await freshGET(makeListRequest());
        // FAILS on M8 (stale marker dropped): status/body would look like an
        // ordinary fresh 200 with no signal the live read timed out.
        expect(second.status).toBe(200);
        const secondBody = await second.json();
        expect(secondBody.stale).toBe(true);
        expect(secondBody.library.skills[0]?.id).toBe("scraper"); // still the last KNOWN-GOOD snapshot
      } finally {
        if (previousDeadlineEnv === undefined) delete process.env.SKILLS_LIST_BUILD_DEADLINE_MS;
        else process.env.SKILLS_LIST_BUILD_DEADLINE_MS = previousDeadlineEnv;
        if (previousCooldownEnv === undefined) delete process.env.SKILLS_LIST_DEADLINE_COOLDOWN_MS;
        else process.env.SKILLS_LIST_DEADLINE_COOLDOWN_MS = previousCooldownEnv;
        if (previousCacheTtlEnv === undefined) delete process.env.SKILLS_LIST_CACHE_TTL_MS;
        else process.env.SKILLS_LIST_CACHE_TTL_MS = previousCacheTtlEnv;
      }
    }, 10_000);
  });

  describe("OC8 — plain-dir SKILL.md existence probe (favourable-absence reintroduced, round 3)", () => {
    it("ENOENT on <dir>/SKILL.md is the ONLY proof of absence — renders a healthy, empty entry", async () => {
      stubReaddir([fakeDirent("absent", "dir")]);
      stubLstat((path) => (path.endsWith("/SKILL.md") ? "ENOENT" : { isSymbolicLink: () => false }));
      execFileImplMock.mockImplementation((file: string) => {
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        // The readlink gate must never be reached for a proven-absent entry.
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();
      const entry = body.library.skills.find((s: { id: string }) => s.id === "absent");
      expect(entry).toBeDefined();
      expect(entry.category).not.toMatch(/^unreadable/);
      expect(entry.description).toBe("");
      expect(entry.symlinkTarget).toBeNull();
    });

    it("EACCES on <dir>/SKILL.md (e.g. a mode-000 directory) renders 'unreadable (error)', never healthy-empty", async () => {
      stubReaddir([fakeDirent("locked", "dir")]);
      stubLstat((path) => (path.endsWith("/SKILL.md") ? "EACCES" : { isSymbolicLink: () => false }));
      execFileImplMock.mockImplementation((file: string) => {
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();
      const entry = body.library.skills.find((s: { id: string }) => s.id === "locked");
      expect(entry).toBeDefined();
      expect(entry.category).toBe("unreadable (error)");
      expect(entry.description).toBe("");
    });

    it("a dangling symlink at <dir>/SKILL.md (lstat succeeds — nofollow never sees the missing target) renders 'unreadable (error)', never healthy-empty", async () => {
      stubReaddir([fakeDirent("dangling", "dir")]);
      // lstat on a DANGLING symlink SUCCEEDS — it never follows the final
      // component, so it can't see that the target is missing. This is
      // exactly the opus-cited case that used to be indistinguishable from
      // genuine absence.
      stubLstat((path) =>
        path.endsWith("/SKILL.md") ? { isSymbolicLink: () => true } : { isSymbolicLink: () => false },
      );
      execFileImplMock.mockImplementation((file: string) => {
        // Verified locally: readlink -fn on a dangling target exits 1.
        if (file === "/usr/bin/readlink") return Promise.reject(execError("readlink"));
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();
      const entry = body.library.skills.find((s: { id: string }) => s.id === "dangling");
      expect(entry).toBeDefined();
      expect(entry.category).toBe("unreadable (error)");
      expect(entry.description).toBe("");
    });

    it("a locked and a healthy plain-dir skill no longer render byte-identically (opus OC8 direct repro shape)", async () => {
      stubReaddir([fakeDirent("locked-skill", "dir"), fakeDirent("healthy-skill", "dir")]);
      stubLstat((path) => {
        if (path.endsWith("/SKILL.md") && path.includes("locked-skill")) return "EACCES";
        return { isSymbolicLink: () => false };
      });
      execFileImplMock.mockImplementation((file: string, args: string[]) => {
        if (file === "/usr/bin/readlink") {
          const target = args[args.length - 1] as string;
          if (target.includes("locked-skill")) {
            return Promise.reject(new Error("readlink must never be called for locked-skill — EACCES was already proven via lstat"));
          }
          return Promise.resolve({ stdout: target, stderr: "" });
        }
        if (file === process.execPath) return Promise.resolve({ stdout: "---\nname: healthy-skill\n---\nbody", stderr: "" });
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();
      const locked = body.library.skills.find((s: { id: string }) => s.id === "locked-skill");
      const healthy = body.library.skills.find((s: { id: string }) => s.id === "healthy-skill");
      const shape = (s: Record<string, unknown>) => ({ ...s, id: "X", name: "X" });
      // FAILS on 857841d2f: shape(locked) equals shape(healthy) apart from id/name.
      expect(shape(locked)).not.toEqual(shape(healthy));
      expect(locked.category).toBe("unreadable (error)");
    });

    it("a SYMLINKED entry's gate failure still never falls back to healthy-empty, keeping M7's intent green", async () => {
      // M7 (OC3b mutation probe) pinned that the OLD `if (!isSymlink && ...)`
      // guard could not be widened to cover symlinked dirs too. That literal
      // code is gone (replaced by this whole OC8 restructure), but the
      // INVARIANT it protected — a symlinked dir's gate failure is always
      // "unreadable"/"refused", never assumed absent — must still hold. The
      // existing "timeout and error gate failures on symlinked entries"
      // test above already proves this for the "error" reason; this test
      // pins the SAME invariant end-to-end via GET for extra safety.
      stubReaddir([fakeDirent("symlinked-broken", "symlink")]);
      lstatMock.mockResolvedValue({ isSymbolicLink: () => true });
      execFileImplMock.mockImplementation((file: string) => {
        if (file === "/usr/bin/readlink") return Promise.reject(execError("readlink"));
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();
      const entry = body.library.skills.find((s: { id: string }) => s.id === "symlinked-broken");
      expect(entry).toBeDefined();
      expect(entry.category).toBe("unreadable (error)");
    });

    it("a SYMLINKED entry's OC8 lstat pre-check must stay skipped even when the literal (unresolved) SKILL.md path would itself lstat as ENOENT (M7 kill)", async () => {
      // A symlinked directory's SKILL.md can only be found by RESOLVING the
      // symlink (readlink -fn) — lstat'ing the literal, unresolved
      // "<symlinked-dir>/SKILL.md" string tells you nothing about whether
      // the REAL target exists, and doing so at all would (in production)
      // reintroduce the very TCC in-process traversal this route exists to
      // avoid. If the `!isSymlink` guard around the OC8 pre-check were ever
      // widened to cover symlinked dirs too, THIS scenario — a literal path
      // that lstats ENOENT locally while the symlink's real target is fully
      // healthy — would wrongly short-circuit to a healthy-EMPTY entry
      // instead of actually resolving and reading it.
      stubReaddir([fakeDirent("scraper", "symlink")]);
      stubLstat((path) => {
        if (path.endsWith("/SKILL.md")) return "ENOENT"; // literal path: nothing there locally
        return { isSymbolicLink: () => true }; // the directory entry itself is the symlink
      });
      const skillMd = "---\nname: Scraper\ndescription: real content via the resolved symlink target\n---\nbody";
      execFileImplMock.mockImplementation((file: string, args: string[]) => {
        // readlink -fn FULLY resolves the symlink chain and succeeds.
        if (file === "/usr/bin/readlink") return Promise.resolve({ stdout: args[1], stderr: "" });
        if (file === process.execPath) return Promise.resolve({ stdout: skillMd, stderr: "" });
        if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
        return unexpectedExec(file);
      });

      const response = await GET(makeListRequest());
      const body = await response.json();
      const entry = body.library.skills.find((s: { id: string }) => s.id === "scraper");
      expect(entry).toBeDefined();
      // FAILS if the OC8 pre-check ever runs for a symlinked entry: it would
      // short-circuit on the literal-path ENOENT and never reach the read at
      // all, rendering description "" instead of the real content.
      expect(entry.description).toBe("real content via the resolved symlink target");
    });
  });

  describe("OC9 — deadline env-override validation (round 3)", () => {
    it("SKILLS_LIST_BUILD_DEADLINE_MS=-1 is clamped to the floor, not honored verbatim — a healthy build still succeeds", async () => {
      const previousEnv = process.env.SKILLS_LIST_BUILD_DEADLINE_MS;
      process.env.SKILLS_LIST_BUILD_DEADLINE_MS = "-1";
      vi.resetModules();
      const { GET: freshGET } = await import("./route");
      try {
        // A REAL readdir costs at least one macrotask tick — resolving via a
        // real setTimeout (not a microtask) matches opus's repro and would
        // expose an unclamped -1ms deadline (setTimeout clamps a negative
        // delay to ~1ms) even though the fs answers in 5ms.
        readdirMock.mockImplementation((dir: string) => {
          if (dir === CLAUDE_SKILLS_DIR) return new Promise((r) => setTimeout(() => r([fakeDirent("fusion", "dir")]), 5));
          if (dir === REPO_SKILLS_DIR) return new Promise((r) => setTimeout(() => r([]), 5));
          return Promise.reject(new Error("unexpected readdir"));
        });
        stubLstat(() => ({ isSymbolicLink: () => false }));
        execFileImplMock.mockImplementation((file: string, args: string[]) => {
          if (file === "/usr/bin/readlink") return Promise.resolve({ stdout: args[1], stderr: "" });
          if (file === process.execPath) {
            return Promise.resolve({ stdout: "---\nname: Fusion\ndescription: real\n---\nbody", stderr: "" });
          }
          if (file === "sqlite3") return Promise.resolve({ stdout: "[]", stderr: "" });
          return unexpectedExec(file);
        });

        const response = await freshGET(makeListRequest());
        const body = await response.json();
        // FAILS on 857841d2f: status 503, library.source "error", 0 skills,
        // on a filesystem that answered every call in 5ms.
        expect(response.status).toBe(200);
        expect(body.library.skills).toHaveLength(1);
      } finally {
        if (previousEnv === undefined) delete process.env.SKILLS_LIST_BUILD_DEADLINE_MS;
        else process.env.SKILLS_LIST_BUILD_DEADLINE_MS = previousEnv;
      }
    });
  });

  describe("OC12 — SIGKILL is pinned by a direct assertion on both exec call sites (round 3)", () => {
    it("both childRealpath's and childRead's execFileAsync calls carry killSignal: SIGKILL", async () => {
      lstatMock.mockResolvedValue({ isSymbolicLink: () => true });
      execFileImplMock.mockImplementation((file: string, args: string[]) => {
        if (file === "/usr/bin/readlink") return Promise.resolve({ stdout: args[1], stderr: "" });
        if (file === process.execPath) return Promise.resolve({ stdout: "---\nname: S\n---\nbody", stderr: "" });
        return unexpectedExec(file);
      });

      await GET(makeDetailRequest("scraper"));

      const readlinkCall = execFileImplMock.mock.calls.find(([file]: unknown[]) => file === "/usr/bin/readlink");
      expect(readlinkCall).toBeDefined();
      expect(readlinkCall![2]).toMatchObject({ killSignal: "SIGKILL" });

      const readCall = execFileImplMock.mock.calls.find(([file]: unknown[]) => file === process.execPath);
      expect(readCall).toBeDefined();
      expect(readCall![2]).toMatchObject({ killSignal: "SIGKILL" });
    });
  });
});

describe("SHOULD 6 / F-TOCTOU-PARENT / F-FIFO-BLOCK — the read script itself, spawned for real (round 2/3)", () => {
  // These tests bypass ALL the vi.mock() setups above (via vi.importActual)
  // and spawn the ACTUAL exported TOCTOU_SAFE_READ_SCRIPT with a REAL,
  // unmocked child_process against a real tmp-dir fixture — proving the
  // script's own refusal behavior directly, not a duplicated copy of it
  // that could silently drift from what actually runs in production.
  it("refuses a last-component symlink swap and a directory; reads a regular file's exact bytes", async () => {
    const { execFile: realExecFile } = await vi.importActual<typeof import("node:child_process")>("node:child_process");
    const { promisify: realPromisify } = await vi.importActual<typeof import("node:util")>("node:util");
    const realExecFileAsync = realPromisify(realExecFile);
    const { mkdtemp, writeFile, symlink, rm } = await vi.importActual<typeof import("node:fs/promises")>("node:fs/promises");
    const { tmpdir } = await vi.importActual<typeof import("node:os")>("node:os");
    const { join: joinPath } = await vi.importActual<typeof import("node:path")>("node:path");
    const { TOCTOU_SAFE_READ_SCRIPT } = await import("./toctou-read-script");

    const dir = await mkdtemp(joinPath(tmpdir(), "toctou-script-test-"));
    try {
      const regular = joinPath(dir, "regular.txt");
      await writeFile(regular, "HELLO\n");
      const link = joinPath(dir, "link.txt");
      await symlink(regular, link);

      // Last-component symlink swap -> refused (ELOOP via O_NOFOLLOW).
      await expect(
        realExecFileAsync(process.execPath, ["-e", TOCTOU_SAFE_READ_SCRIPT, "--", link], {
          timeout: 3000,
          encoding: "utf8",
        }),
      ).rejects.toMatchObject({ code: 1 });

      // A directory -> refused (not a regular file).
      await expect(
        realExecFileAsync(process.execPath, ["-e", TOCTOU_SAFE_READ_SCRIPT, "--", dir], {
          timeout: 3000,
          encoding: "utf8",
        }),
      ).rejects.toMatchObject({ code: 1 });

      // A regular file -> succeeds, exact bytes.
      const { stdout } = await realExecFileAsync(process.execPath, ["-e", TOCTOU_SAFE_READ_SCRIPT, "--", regular], {
        timeout: 3000,
        encoding: "utf8",
      });
      expect(stdout).toBe("HELLO\n");
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it("opens a FIFO's read side promptly and refuses it instead of blocking to the SIGKILL deadline (F-FIFO-BLOCK)", async () => {
    const { execFile: realExecFile } = await vi.importActual<typeof import("node:child_process")>("node:child_process");
    const { promisify: realPromisify } = await vi.importActual<typeof import("node:util")>("node:util");
    const realExecFileAsync = realPromisify(realExecFile);
    const { mkdtemp, rm } = await vi.importActual<typeof import("node:fs/promises")>("node:fs/promises");
    const { tmpdir } = await vi.importActual<typeof import("node:os")>("node:os");
    const { join: joinPath } = await vi.importActual<typeof import("node:path")>("node:path");
    const { execFileSync } = await vi.importActual<typeof import("node:child_process")>("node:child_process");
    const { TOCTOU_SAFE_READ_SCRIPT } = await import("./toctou-read-script");

    const dir = await mkdtemp(joinPath(tmpdir(), "toctou-fifo-test-"));
    try {
      const fifo = joinPath(dir, "as_fifo");
      execFileSync("/usr/bin/mkfifo", [fifo]);

      const started = Date.now();
      await expect(
        realExecFileAsync(process.execPath, ["-e", TOCTOU_SAFE_READ_SCRIPT, "--", fifo], {
          timeout: 3000,
          encoding: "utf8",
        }),
      ).rejects.toMatchObject({ code: 1 });
      const elapsedMs = Date.now() - started;
      // FAILS without O_NONBLOCK: opening a FIFO's read side blocks until a
      // writer connects — up to the full 3s SIGKILL timeout.
      expect(elapsedMs).toBeLessThan(1000);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
