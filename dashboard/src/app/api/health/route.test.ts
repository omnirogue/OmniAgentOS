// @vitest-environment node
//
// Mirrors the mocking pattern used by src/app/api/local/skills-extra/route.test.ts:
// execFile is mocked via its util.promisify.custom implementation (matching how
// Node's real child_process.execFile promisifies), and node:fs/promises is
// mocked so this stays hermetic — it never actually shells out to python3 or
// touches the real capmap registry/state on disk.
import type { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { execFileImplMock, readFileMock, readdirMock } = vi.hoisted(() => ({
  execFileImplMock: vi.fn(),
  readFileMock: vi.fn(),
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

vi.mock("node:fs/promises", async () => {
  const actual = await vi.importActual<typeof import("node:fs/promises")>("node:fs/promises");
  return { ...actual, readFile: readFileMock, readdir: readdirMock };
});

function makeRequest(query = ""): NextRequest {
  const url = new URL(`http://dashboard.test/api/health${query}`);
  const request = new Request(url) as NextRequest;
  Object.defineProperty(request, "nextUrl", { value: url, configurable: true });
  return request;
}

const STATUS_ROWS = [
  { company: "estate", id: "airtop", status: "OK", last_verified: "2026-08-15T14:57:14Z" },
  { company: "estate", id: "loop-cadence", status: "DOWN", last_verified: "2026-08-15T15:07:41Z" },
  { company: "acmeuni", id: "name-com", status: "UNVERIFIED", last_verified: null },
];

const LIST_ROWS = [
  { company: "estate", id: "airtop", kind: "external-service", what_it_does: "Airtop reachability." },
  { company: "estate", id: "loop-cadence", kind: "mechanical-automation", what_it_does: "Loop cadence check." },
  { company: "acmeuni", id: "name-com", kind: "external-service", what_it_does: "name.com reachability." },
];

const RUNS_JSONL = [
  { ts: "2026-08-15T14:57:14Z", capability_id: "airtop", status: "OK", exit_code: 0, latency_ms: 12, evidence: null, metric: { verification_type: "snapshot_field" } },
  { ts: "2026-08-15T15:00:00Z", capability_id: "loop-cadence", status: "OK", exit_code: 0, latency_ms: 5, evidence: null, metric: { verification_type: "command" } },
  { ts: "2026-08-15T15:07:41Z", capability_id: "loop-cadence", status: "DOWN", exit_code: 2, latency_ms: 8, evidence: "/tmp/evidence.json", metric: { verification_type: "command" } },
]
  .map((r) => JSON.stringify(r))
  .join("\n") + "\n";

const STATE_JSON = JSON.stringify({
  airtop: { status: "OK", last_verified: "2026-08-15T14:57:14Z", evidence: "/tmp/airtop.json", latency_ms: 0, updated_at: "2026-08-15T15:07:41Z" },
  "loop-cadence": { status: "DOWN", last_verified: "2026-08-15T15:07:41Z", evidence: "/tmp/loop-cadence.json", latency_ms: 8, updated_at: "2026-08-15T15:07:41Z" },
});

const REGISTRY_FILES: Record<string, unknown> = {
  "airtop.json": {
    id: "airtop",
    owner: "estate-ops",
    verification: { type: "snapshot_field", source: "/Users/youruser/Work/Ops/var/integrations-health.json", field: "integrations.airtop.status" },
  },
  "loop-cadence.json": {
    id: "loop-cadence",
    owner: "gate-team",
    verification: { type: "command", argv: ["capmap-probe", "loop-cadence"] },
  },
  "name-com.json": {
    id: "name-com",
    owner: "estate-ops",
    verification: { type: "unset" },
  },
};

function setupMocks() {
  execFileImplMock.mockImplementation((file: string, args: string[]) => {
    if (file !== "python3") return Promise.reject(new Error(`unexpected exec: ${file}`));
    if (args.includes("status")) return Promise.resolve({ stdout: JSON.stringify(STATUS_ROWS), stderr: "" });
    if (args.includes("list")) return Promise.resolve({ stdout: JSON.stringify(LIST_ROWS), stderr: "" });
    return Promise.reject(new Error(`unexpected capmap subcommand: ${args.join(" ")}`));
  });
  readdirMock.mockImplementation((dir: string) => {
    if (dir.endsWith("capabilities")) return Promise.resolve(Object.keys(REGISTRY_FILES));
    return Promise.reject(Object.assign(new Error("ENOENT"), { code: "ENOENT" }));
  });
  readFileMock.mockImplementation((path: string) => {
    for (const [filename, contents] of Object.entries(REGISTRY_FILES)) {
      if (path.endsWith(filename)) return Promise.resolve(JSON.stringify(contents));
    }
    if (path.endsWith("state.json")) return Promise.resolve(STATE_JSON);
    if (path.endsWith("runs.jsonl")) return Promise.resolve(RUNS_JSONL);
    return Promise.reject(Object.assign(new Error("ENOENT"), { code: "ENOENT" }));
  });
}

describe("GET /api/health", () => {
  let GET: typeof import("./route").GET;

  beforeEach(async () => {
    execFileImplMock.mockReset();
    readFileMock.mockReset();
    readdirMock.mockReset();
    vi.resetModules();
    ({ GET } = await import("./route"));
    setupMocks();
  });

  it("renders what the CLI computed (status/last_verified) verbatim, merged with registry metadata — never recomputing status", async () => {
    const response = await GET(makeRequest());
    const body = await response.json();

    expect(response.status).toBe(200);
    const byId = Object.fromEntries(body.capabilities.map((c: { id: string }) => [c.id, c]));

    expect(byId.airtop.status).toBe("OK");
    expect(byId.airtop.kind).toBe("external-service");
    expect(byId.airtop.owner).toBe("estate-ops");
    expect(byId.airtop.last_checked).toBe("2026-08-15T14:57:14Z");
    expect(byId.airtop.evidence).toBe("/tmp/airtop.json");

    expect(byId["loop-cadence"].status).toBe("DOWN");
    expect(byId["loop-cadence"].owner).toBe("gate-team");

    expect(byId["name-com"].status).toBe("UNVERIFIED");
    expect(byId["name-com"].owner).toBe("estate-ops");
  });

  it("derives last_good from the most recent OK run in runs.jsonl, distinct from last_checked", async () => {
    const response = await GET(makeRequest());
    const body = await response.json();
    const byId = Object.fromEntries(body.capabilities.map((c: { id: string }) => [c.id, c]));

    // loop-cadence's last CHECK was the DOWN run at 15:07:41, but its last GOOD
    // run was earlier, at 15:00:00 — these must not collapse into one field.
    expect(byId["loop-cadence"].last_checked).toBe("2026-08-15T15:07:41Z");
    expect(byId["loop-cadence"].last_good).toBe("2026-08-15T15:00:00Z");
  });

  it("never crashes or fabricates a status when a capability has no run history yet (UNVERIFIED, last_good null)", async () => {
    const response = await GET(makeRequest());
    const body = await response.json();
    const byId = Object.fromEntries(body.capabilities.map((c: { id: string }) => [c.id, c]));
    expect(byId["name-com"].last_good).toBeNull();
  });

  it("?id= returns a drill-in detail payload with recent run history and a human-readable verification command", async () => {
    const response = await GET(makeRequest("?id=loop-cadence"));
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.id).toBe("loop-cadence");
    expect(body.verification_command).toBe("capmap-probe loop-cadence");
    expect(body.recent_runs).toHaveLength(2);
    // newest first
    expect(body.recent_runs[0].status).toBe("DOWN");
    expect(body.recent_runs[1].status).toBe("OK");
    expect(body.last_error.status).toBe("DOWN");
    expect(body.last_error.exit_code).toBe(2);
  });

  it("?id= for an unknown capability returns 404, not a fabricated empty row", async () => {
    const response = await GET(makeRequest("?id=does-not-exist"));
    expect(response.status).toBe(404);
  });

  it("describes a snapshot_field verification spec by field + source, never the credential value itself", async () => {
    const response = await GET(makeRequest("?id=airtop"));
    const body = await response.json();
    expect(body.verification_command).toContain("integrations.airtop.status");
    expect(body.verification_command).toContain("integrations-health.json");
  });

  it("a CLI exec failure surfaces as a 503 with a readable message, not a silently-empty 200", async () => {
    execFileImplMock.mockImplementation(() => Promise.reject(Object.assign(new Error("boom"), { killed: false })));
    const response = await GET(makeRequest());
    expect(response.status).toBe(503);
    const body = await response.json();
    expect(typeof body.error).toBe("string");
  });
});
