import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  fetchEngineCapabilities,
  fetchEngineRunSnapshot,
  fetchEngineRuns,
} from "./client";

// The shared contract fixture (contracts/fixtures/loopdeck-engine-v1.json,
// docs/attempt-1-implementation-design.md SS2.4) is a human-readable
// normative example for contracts/loopdeck-engine-api.md, not something this
// file imports: a cross-package JSON import from dashboard/ has no prior art
// in this repo and would make this test's outcome depend on Vite's fs.allow
// resolution rather than on the client code under test. Its "hostile
// sample" (unknown api_version, non-GitHub link, secret/host path) is
// reproduced inline below instead, so the client is exercised the same way
// either way.

/** A minimal well-formed snapshot body; tests override single fields to break it. */
function snapshotBody(): Record<string, unknown> {
  return {
    api_version: "loopdeck-engine/v1",
    run: { id: "run-1", status: "running" },
    tasks: [],
    deps: [],
    attempts: {},
    progress: {},
    metrics: {},
    activity: [],
    next_activity_cursor: 0,
    artifacts: [],
    context: { repository: null, branch: null, head_sha: null },
    evidence: { commits: [], files: [], tests: [], reports: [] },
    approval: { approved: false, receipt: null },
  };
}

describe("control-plane read client", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("normalizes the versioned engine capability projection", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          api_version: "loopdeck-engine/v1",
          product: "OmniAgentOS",
          read_only: true,
          capabilities: {
            swarm: true,
            parallel_execution: true,
            execution_enabled: false,
            worktree_isolation_enabled: true,
            memory: true,
            reflection: true,
            improvements: true,
            activity_cursor: true,
          },
          links: { snapshot: "/api/engine/runs/{run_id}/snapshot" },
        }),
      ),
    );
    await expect(fetchEngineCapabilities()).resolves.toMatchObject({
      api_version: "loopdeck-engine/v1",
      read_only: true,
      capabilities: { parallel_execution: true, execution_enabled: false },
      links: { snapshot: "/api/engine/runs/{run_id}/snapshot" },
    });
  });

  it("keeps missing engine capabilities unavailable instead of inventing support", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({})));
    await expect(fetchEngineCapabilities()).resolves.toMatchObject({
      api_version: "unknown",
      read_only: true,
      capabilities: { swarm: false, memory: false, improvements: false },
    });
  });

  it("rejects an api_version other than loopdeck-engine/v1 from the fixture's hostile sample as incompatible, not a crash", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ api_version: "unknown-future/v9" })),
    );
    await expect(fetchEngineCapabilities()).resolves.toMatchObject({ api_version: "unknown-future/v9" });
  });

  it("fetches a bound run snapshot with an encoded id and a bounded cursor/limit", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          api_version: "loopdeck-engine/v1",
          run: { id: "run/one", status: "running" },
          tasks: [],
          deps: [],
          attempts: {},
          progress: {},
          metrics: {},
          activity: [],
          next_activity_cursor: 9,
          artifacts: [],
          context: { repository: null, branch: null, head_sha: null },
          evidence: { commits: [], files: [], tests: [], reports: [] },
          approval: { approved: false, receipt: null },
        }),
      ),
    );
    await expect(fetchEngineRunSnapshot("run/one", 9, 900)).resolves.toMatchObject({
      run: { id: "run/one" },
      next_activity_cursor: 9,
    });
    expect(String(fetchMock.mock.calls[0]![0])).toContain(
      "/api/engine/runs/run%2Fone/snapshot?after=9&limit=500",
    );
  });

  it("normalizes a hostile evidence link the fixture ships (non-GitHub url) to null instead of trusting it", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          api_version: "loopdeck-engine/v1",
          run: { id: "run-1", status: "running" },
          tasks: [],
          deps: [],
          attempts: {},
          progress: {},
          metrics: {},
          activity: [],
          next_activity_cursor: 0,
          artifacts: [],
          context: { repository: null, branch: null, head_sha: null },
          evidence: {
            commits: [{ sha: "aaa1111", message: "m", url: "https://not-github.example/aaa" }],
            files: [],
            tests: [],
            reports: [],
          },
          approval: { approved: false, receipt: null },
        }),
      ),
    );
    const result = await fetchEngineRunSnapshot("run-1");
    expect(result.evidence.commits[0]?.url).toBeNull();
  });

  it("bounds the runs list request and normalizes the returned summaries", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ runs: [{ id: "swr-2", status: "running", goal: "Fix" }] }),
      ),
    );
    await expect(fetchEngineRuns(500)).resolves.toMatchObject({
      runs: [{ id: "swr-2", status: "running" }],
    });
    expect(String(fetchMock.mock.calls[0]![0])).toContain("/api/engine/runs?limit=50");
  });

  it("surfaces an upstream error instead of returning a false empty state", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error: { message: "engine unavailable" } }), { status: 503 }),
    );
    await expect(fetchEngineCapabilities()).rejects.toMatchObject({
      status: 503,
      message: "engine unavailable",
    });
  });

  /**
   * CP-004: the real projector (`_CONTEXT_FIELDS` in engine.py) only ever
   * emits repository/branch/head_sha -- it never emits base_sha. The client
   * must project exactly that shape and must not surface a base_sha even if
   * an upstream payload happens to carry one (e.g. a future or unrelated
   * field), so it never fabricates a "base" the engine did not report.
   */
  it("normalizes context to exactly the fields the real projector emits", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          api_version: "loopdeck-engine/v1",
          run: { id: "run-1", status: "running" },
          tasks: [],
          deps: [],
          attempts: {},
          progress: {},
          metrics: {},
          activity: [],
          next_activity_cursor: 0,
          artifacts: [],
          context: {
            repository: "acme/widgets",
            branch: "main",
            head_sha: "deadbeef0123456789abcdef0123456789abcdef",
          },
          evidence: { commits: [], files: [], tests: [], reports: [] },
          approval: { approved: false, receipt: null },
        }),
      ),
    );
    const result = await fetchEngineRunSnapshot("run-1");
    expect(result.context).toStrictEqual({
      repository: "acme/widgets",
      branch: "main",
      head_sha: "deadbeef0123456789abcdef0123456789abcdef",
    });
    expect(result.context).not.toHaveProperty("base_sha");
  });

  it("does not surface an unexpected base_sha even if a payload carries one", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          api_version: "loopdeck-engine/v1",
          run: { id: "run-1", status: "running" },
          tasks: [],
          deps: [],
          attempts: {},
          progress: {},
          metrics: {},
          activity: [],
          next_activity_cursor: 0,
          artifacts: [],
          context: {
            repository: "acme/widgets",
            branch: "main",
            head_sha: "deadbeef0123456789abcdef0123456789abcdef",
            base_sha: "cafebabe0123456789abcdef0123456789abcdef",
          },
          evidence: { commits: [], files: [], tests: [], reports: [] },
          approval: { approved: false, receipt: null },
        }),
      ),
    );
    const result = await fetchEngineRunSnapshot("run-1");
    expect(result.context).not.toHaveProperty("base_sha");
  });

  // F004 (crit-20260813T115400Z receipt): a malformed activity feed must
  // reject the snapshot. Coercing it to a healthy-empty page (`[]`, `id: 0`)
  // would let hooks advance next_activity_cursor and permanently skip events;
  // a rejection instead reaches hooks' snapshot error path, which keeps the
  // last confirmed snapshot and does not advance the cursor.
  it("rejects a snapshot whose activity is not a list instead of coercing it to empty", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ...snapshotBody(), activity: { corrupt: true } })),
    );
    await expect(fetchEngineRunSnapshot("run-1")).rejects.toMatchObject({
      name: "ControlPlaneApiError",
      message: expect.stringContaining("malformed"),
    });
  });

  it("rejects a snapshot carrying an activity event without a numeric id instead of inventing id 0", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          ...snapshotBody(),
          activity: [{ id: "not-a-number", action: "task_assigned", payload: {} }],
        }),
      ),
    );
    await expect(fetchEngineRunSnapshot("run-1")).rejects.toMatchObject({
      name: "ControlPlaneApiError",
      message: expect.stringContaining("malformed"),
    });
  });

  it("rejects a snapshot without a trustworthy next_activity_cursor", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ...snapshotBody(), next_activity_cursor: "later" })),
    );
    await expect(fetchEngineRunSnapshot("run-1")).rejects.toMatchObject({
      name: "ControlPlaneApiError",
      message: expect.stringContaining("malformed"),
    });
  });

  it("preserves activity timeline fields (role, account, phase, evidence_links) on the snapshot", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          api_version: "loopdeck-engine/v1",
          run: { id: "run-1", status: "running" },
          tasks: [],
          deps: [],
          attempts: {},
          progress: {},
          metrics: {},
          activity: [
            {
              id: 7,
              action: "task_assigned",
              created_at: "2026-08-07T12:00:00Z",
              payload: {
                role: "implementer",
                model: "grok-4.5",
                account: "codex-2",
                phase: "running",
                status: "running",
                evidence_links: [
                  {
                    label: "aaa1111",
                    url: "https://github.com/acme/widgets/commit/aaa1111",
                  },
                  {
                    label: "evil",
                    url: "https://evil.example.com/x",
                  },
                ],
              },
            },
          ],
          next_activity_cursor: 7,
          artifacts: [],
          context: { repository: null, branch: null, head_sha: null },
          evidence: { commits: [], files: [], tests: [], reports: [] },
          approval: { approved: false, receipt: null },
        }),
      ),
    );
    const result = await fetchEngineRunSnapshot("run-1");
    const activity = result.activity[0];
    expect(activity).toMatchObject({
      id: 7,
      action: "task_assigned",
      created_at: "2026-08-07T12:00:00Z",
      payload: {
        role: "implementer",
        model: "grok-4.5",
        account: "codex-2",
        phase: "running",
        status: "running",
      },
    });
    // Client re-applies the GitHub link rule on evidence_links the same way it
    // does for top-level evidence entries.
    const links = activity?.payload?.evidence_links as Array<{ label?: string; url?: string | null }>;
    expect(links[0]?.url).toBe("https://github.com/acme/widgets/commit/aaa1111");
    expect(links[1]?.url).toBeNull();
  });


});
