// LANDMINE 2 (docs/attempt-1-implementation-design.md SS1): this environment
// exports NODE_ENV=production, which makes React resolve its production
// build inside vitest and throws "React.act is not a function" on every RTL
// render. vi.hoisted runs before this file's static imports (react-dom's CJS
// entry point reads NODE_ENV at import time), so this must stay first.
// Causally verified against a trivial render probe before writing the
// scenarios below (.loopdeck/test-evidence.json).
vi.hoisted(() => {
  (process.env as Record<string, string>).NODE_ENV = "test";
});

import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ControlPlaneView } from "./ControlPlaneView";
import { useControlPlane } from "./hooks";
import styles from "./controlplane.module.css";

vi.mock("./hooks", () => ({ useControlPlane: vi.fn() }));

const mocked = vi.mocked(useControlPlane);

// Trimmed hook-return contract the builder's hooks.ts/ControlPlaneView.tsx
// must implement (this feature has no prior art on this branch to read, so
// this test file IS the interface spec, per docs/attempt-1-implementation-
// design.md SS3 and SS4):
//   runs: EngineRunSummary[]                 -- from GET /api/engine/runs
//   runId: string | null                     -- user-selected run, else newest
//   setRunId: (id: string) => void
//   capabilities: EngineCapabilities | null
//   capabilitiesError: string | null
//   snapshot: EngineRunSnapshot | null
//   snapshotError: string | null
//   loading: boolean
//   hasLoaded: boolean
//   error: string | null                     -- blocks first paint (runs list failed)
//   stale: boolean
//   refresh: () => void
function state(overrides: Record<string, unknown> = {}) {
  return {
    runs: [],
    runId: null,
    setRunId: vi.fn(),
    capabilities: null,
    capabilitiesError: null,
    snapshot: null,
    snapshotError: null,
    loading: false,
    hasLoaded: true,
    error: null,
    stale: false,
    refresh: vi.fn(),
    ...overrides,
  } as ReturnType<typeof useControlPlane>;
}

const CAPABILITIES = {
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
  links: {
    create_run: "/api/swarm",
    run: null,
    activity: null,
    snapshot: null,
    memory_search: null,
    improvements: null,
  },
};

function snapshot(overrides: Record<string, unknown> = {}) {
  return {
    api_version: "loopdeck-engine/v1",
    run: { id: "swr-1", status: "running", goal: "Ship the adapter" },
    tasks: [],
    deps: [],
    attempts: {},
    progress: {},
    metrics: {},
    activity: [],
    next_activity_cursor: 0,
    artifacts: [],
    context: { repository: "acme/widgets", branch: "main", head_sha: "deadbeef0123" },
    evidence: { commits: [], files: [], tests: [], reports: [] },
    approval: { approved: false, receipt: null },
    ...overrides,
  };
}

describe("ControlPlaneView", () => {
  beforeEach(() => mocked.mockReset());

  it("shows a loading state before any data has loaded", () => {
    mocked.mockReturnValue(state({ loading: true, hasLoaded: false }));
    render(<ControlPlaneView />);
    expect(screen.getByRole("status", { name: /loading/i })).toBeInTheDocument();
  });

  it("shows a blocking error before any confirmed data", () => {
    mocked.mockReturnValue(state({ hasLoaded: false, error: "API unavailable" }));
    render(<ControlPlaneView />);
    expect(screen.getByText("API unavailable")).toBeInTheDocument();
  });

  it("marks retained data stale after a refresh error", () => {
    mocked.mockReturnValue(state({ stale: true, error: "refresh failed" }));
    render(<ControlPlaneView />);
    expect(screen.getByText("The latest refresh failed. Showing the last confirmed snapshot.")).toBeInTheDocument();
  });

  it("shows a truthful empty state when there are no LoopDeck runs", () => {
    mocked.mockReturnValue(state({ runs: [] }));
    render(<ControlPlaneView />);
    expect(screen.getByText("No LoopDeck runs")).toBeInTheDocument();
  });

  it("shows 'No engine run bound' when runs exist but none is bound", () => {
    mocked.mockReturnValue(
      state({ runs: [{ id: "swr-1", status: "running", goal: "Ship" }], runId: null }),
    );
    render(<ControlPlaneView />);
    expect(screen.getByText("No engine run bound")).toBeInTheDocument();
  });

  it("shows a truthful unknown engine state when capabilities are absent", () => {
    mocked.mockReturnValue(state({ capabilitiesError: "LoopDeck engine status is unavailable." }));
    render(<ControlPlaneView />);
    expect(screen.getByText("Status unknown")).toBeInTheDocument();
    expect(screen.getByText("LoopDeck engine status is unavailable.")).toBeInTheDocument();
  });

  it("renders the versioned engine connection without inventing activity", () => {
    mocked.mockReturnValue(state({ capabilities: CAPABILITIES }));
    render(<ControlPlaneView />);
    expect(screen.getByText("connected")).toBeInTheDocument();
    expect(screen.getByText(/Availability does not imply active work/)).toBeInTheDocument();
  });

  it("does not present an unsupported schema as connected", () => {
    mocked.mockReturnValue(
      state({ capabilities: { ...CAPABILITIES, api_version: "future/v9" } }),
    );
    render(<ControlPlaneView />);
    expect(screen.getByText("incompatible")).toBeInTheDocument();
    expect(screen.queryByText("connected")).not.toBeInTheDocument();
  });

  it("lets the operator select a bound run from the runs list", () => {
    const setRunId = vi.fn();
    mocked.mockReturnValue(
      state({
        runs: [
          { id: "swr-1", status: "running", goal: "Ship the adapter" },
          { id: "swr-2", status: "done", goal: "Fix the flake" },
        ],
        runId: "swr-1",
        setRunId,
        snapshot: snapshot(),
      }),
    );
    render(<ControlPlaneView />);
    fireEvent.click(screen.getByRole("button", { name: /Ship the adapter/ }));
    fireEvent.click(screen.getByText("Fix the flake"));
    expect(setRunId).toHaveBeenCalledWith("swr-2");
  });

  it("renders repository/branch/head-SHA context, treating unset fields honestly", () => {
    mocked.mockReturnValue(
      state({
        runs: [{ id: "swr-1", status: "running", goal: "Ship the adapter" }],
        runId: "swr-1",
        snapshot: snapshot({ context: { repository: "acme/widgets", branch: null, head_sha: null } }),
      }),
    );
    render(<ControlPlaneView />);
    expect(screen.getByText("acme/widgets")).toBeInTheDocument();
    expect(screen.getAllByText("unknown").length).toBeGreaterThanOrEqual(2);
  });

  it("renders approval only from the receipt object, never implied", () => {
    mocked.mockReturnValue(
      state({
        runs: [{ id: "swr-1", status: "running", goal: "Ship the adapter" }],
        runId: "swr-1",
        snapshot: snapshot({
          evidence: {
            commits: [],
            files: [],
            tests: [{ name: "test_a", status: "passed" }],
            reports: [],
          },
          artifacts: [{ id: "art-1", artifact_type: "review" }],
        }),
      }),
    );
    render(<ControlPlaneView />);
    expect(screen.getByText("No approval recorded")).toBeInTheDocument();
  });

  it("renders a receipt-bound approval", () => {
    mocked.mockReturnValue(
      state({
        runs: [{ id: "swr-1", status: "running", goal: "Ship the adapter" }],
        runId: "swr-1",
        snapshot: snapshot({
          approval: { approved: true, receipt: { reviewer: "alice", run_id: "swr-1", head_sha: "deadbeef0123" } },
        }),
      }),
    );
    render(<ControlPlaneView />);
    expect(screen.getByText(/Approved by alice/)).toBeInTheDocument();
    expect(screen.queryByText("No approval recorded")).not.toBeInTheDocument();
  });

  it("renders agent conversation activity as inert text, never as HTML", () => {
    mocked.mockReturnValue(
      state({
        runs: [{ id: "swr-1", status: "running", goal: "Ship the adapter" }],
        runId: "swr-1",
        snapshot: snapshot({
          activity: [
            {
              id: 1,
              action: "attempt.started",
              created_at: "2026-08-07T12:00:00Z",
              payload: { note: '<img src=x onerror="alert(1)">' },
            },
          ],
        }),
      }),
    );
    const { container } = render(<ControlPlaneView />);
    // Raw payload is collapsed by default; open the disclosure to inspect it.
    const disclosure = screen.getByText(/raw payload/i);
    fireEvent.click(disclosure);
    expect(screen.getByText('<img src=x onerror="alert(1)">')).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });

  it("renders a readable activity timeline with role, model/account, timestamp, phase, action/result", () => {
    mocked.mockReturnValue(
      state({
        runs: [{ id: "swr-1", status: "running", goal: "Ship the adapter" }],
        runId: "swr-1",
        snapshot: snapshot({
          activity: [
            {
              id: 1,
              action: "task_assigned",
              created_at: "2026-08-07T12:00:00Z",
              payload: {
                role: "implementer",
                model: "grok-4.5",
                account: "codex-2",
                phase: "running",
                status: "running",
                reason: "slot admitted",
              },
            },
            {
              id: 2,
              action: "review_confirmed",
              created_at: "2026-08-07T12:05:00Z",
              payload: {
                role: "reviewer",
                model: "claude-opus",
                account: "claude-1",
                phase: "reviewing",
                status: "confirmed",
              },
            },
          ],
        }),
      }),
    );
    render(<ControlPlaneView />);

    const timeline = screen.getByRole("list", { name: /agent activity timeline/i });
    expect(timeline).toBeInTheDocument();

    // Timestamp (deterministic UTC label from the pure projector).
    expect(screen.getByText("2026-08-07 12:00:00 UTC")).toBeInTheDocument();
    expect(screen.getByText("2026-08-07 12:05:00 UTC")).toBeInTheDocument();

    // Role + model/account.
    expect(screen.getByText("implementer")).toBeInTheDocument();
    expect(screen.getByText(/grok-4\.5\s*·\s*codex-2/)).toBeInTheDocument();
    expect(screen.getByText("reviewer")).toBeInTheDocument();
    expect(screen.getByText(/claude-opus\s*·\s*claude-1/)).toBeInTheDocument();

    // Phase + concise action/result (not a full payload dump).
    expect(screen.getAllByText("running").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("reviewing")).toBeInTheDocument();
    expect(screen.getByText(/task_assigned/)).toBeInTheDocument();
    expect(screen.getByText(/review_confirmed/)).toBeInTheDocument();
  });

  it("keeps raw payloads collapsed by default and expands them on demand", () => {
    mocked.mockReturnValue(
      state({
        runs: [{ id: "swr-1", status: "running", goal: "Ship the adapter" }],
        runId: "swr-1",
        snapshot: snapshot({
          activity: [
            {
              id: 1,
              action: "task_assigned",
              created_at: "2026-08-07T12:00:00Z",
              payload: {
                role: "implementer",
                model: "grok-4.5",
                task_id: "task-secret-id-xyz",
                note: "operator-only detail",
              },
            },
          ],
        }),
      }),
    );
    render(<ControlPlaneView />);

    // Concise fields are visible without expanding.
    expect(screen.getByText("implementer")).toBeInTheDocument();
    expect(screen.getByText(/grok-4\.5/)).toBeInTheDocument();

    // Raw payload keys/values stay hidden until the disclosure is opened.
    expect(screen.queryByText("task-secret-id-xyz")).not.toBeInTheDocument();
    expect(screen.queryByText("operator-only detail")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText(/raw payload/i));
    expect(screen.getByText("task-secret-id-xyz")).toBeInTheDocument();
    expect(screen.getByText("operator-only detail")).toBeInTheDocument();
  });

  it("renders GitHub evidence links on the timeline and keeps non-GitHub urls inert", () => {
    mocked.mockReturnValue(
      state({
        runs: [{ id: "swr-1", status: "running", goal: "Ship the adapter" }],
        runId: "swr-1",
        snapshot: snapshot({
          activity: [
            {
              id: 1,
              action: "evidence.reported",
              created_at: "2026-08-07T12:00:00Z",
              payload: {
                phase: "running",
                evidence_links: [
                  {
                    label: "aaa1111",
                    url: "https://github.com/acme/widgets/commit/aaa1111",
                  },
                  {
                    label: "untrusted",
                    url: "https://evil.example.com/aaa",
                  },
                ],
              },
            },
          ],
        }),
      }),
    );
    render(<ControlPlaneView />);

    const githubLink = screen.getByRole("link", { name: /aaa1111/ });
    expect(githubLink).toHaveAttribute(
      "href",
      "https://github.com/acme/widgets/commit/aaa1111",
    );
    // F005: the refused link's label stays visible as inert evidence —
    // distinguishable from "no evidence" — but never as an anchor.
    expect(screen.queryByRole("link", { name: /untrusted/ })).not.toBeInTheDocument();
    expect(screen.getByText("untrusted")).toBeInTheDocument();
  });

  it("shows an honest empty state when the bound run has no activity events", () => {
    mocked.mockReturnValue(
      state({
        runs: [{ id: "swr-1", status: "running", goal: "Ship the adapter" }],
        runId: "swr-1",
        snapshot: snapshot({ activity: [] }),
      }),
    );
    render(<ControlPlaneView />);
    expect(screen.getByText(/no agent (messages|activity) recorded/i)).toBeInTheDocument();
  });

  it("renders commits/files/tests/reports evidence sections", () => {
    mocked.mockReturnValue(
      state({
        runs: [{ id: "swr-1", status: "running", goal: "Ship the adapter" }],
        runId: "swr-1",
        snapshot: snapshot({
          evidence: {
            commits: [{ sha: "aaa1111", message: "fix the thing", url: "https://github.com/acme/widgets/commit/aaa1111" }],
            files: [{ path: "omniagentos/api/routes/engine.py", url: null }],
            tests: [{ name: "test_snapshot_ok", status: "passed" }],
            reports: [{ name: "coverage", url: "https://github.com/acme/widgets/actions/runs/1" }],
          },
        }),
      }),
    );
    render(<ControlPlaneView />);
    expect(screen.getByText("aaa1111")).toBeInTheDocument();
    expect(screen.getByText("fix the thing")).toBeInTheDocument();
    expect(screen.getByText("omniagentos/api/routes/engine.py")).toBeInTheDocument();
    expect(screen.getByText("test_snapshot_ok")).toBeInTheDocument();
    expect(screen.getByText("coverage")).toBeInTheDocument();
  });

  it("renders a GitHub evidence link as an anchor and a non-GitHub link as plain text", () => {
    mocked.mockReturnValue(
      state({
        runs: [{ id: "swr-1", status: "running", goal: "Ship the adapter" }],
        runId: "swr-1",
        snapshot: snapshot({
          evidence: {
            commits: [
              { sha: "aaa1111", message: "github commit", url: "https://github.com/acme/widgets/commit/aaa1111" },
              { sha: "bbb2222", message: "untrusted commit", url: "https://evil.example.com/aaa" },
            ],
            files: [],
            tests: [],
            reports: [],
          },
        }),
      }),
    );
    render(<ControlPlaneView />);
    const githubLink = screen.getByRole("link", { name: /aaa1111|github commit/ });
    expect(githubLink).toHaveAttribute("href", "https://github.com/acme/widgets/commit/aaa1111");
    expect(screen.queryByRole("link", { name: /bbb2222|untrusted commit/ })).not.toBeInTheDocument();
    expect(screen.getByText("untrusted commit")).toBeInTheDocument();
  });

  it("applies the wrap-enabling module class to long evidence values (jsdom cannot evaluate the 680px media query — class presence is the assertable limit)", () => {
    mocked.mockReturnValue(
      state({
        runs: [{ id: "swr-1", status: "running", goal: "Ship the adapter" }],
        runId: "swr-1",
        snapshot: snapshot({
          evidence: {
            commits: [],
            files: [
              {
                path: "omniagentos/very/long/nested/path/that/could/overflow/a/phone/width/column/module.py",
                url: null,
              },
            ],
            tests: [],
            reports: [],
          },
        }),
      }),
    );
    const { container } = render(<ControlPlaneView />);
    expect(container.querySelector(`.${styles.wrapValue}`)).not.toBeNull();
  });
});

/**
 * Visible-evidence contract (project/grok-dashboard-visible-evidence-0812).
 *
 * Every Loop card and the Loop detail view must show live binding data —
 * repository, branch, current/base SHA, commits, verification state, agent
 * conversation, and clickable GitHub commit/branch/PR links — never inventing
 * a URL the API did not justify.
 *
 * These scenarios are deliberately RED against the pre-implementer UI (select-
 * only run list, plain-text context, "Head SHA" only, no verification band,
 * no derived GitHub anchors). Implementer lands product code only.
 *
 * Accessibility contract for new chrome (so assertions stay mechanical):
 *   - Loop cards: role=listitem OR role=article, named by goal, showing status.
 *   - Verification summary: role=status with accessible name matching /verification/i
 *     so incidental test-row "passed"/"failed" text cannot satisfy the assert.
 *   - Context labels: exact "Current SHA" (not only legacy "Head SHA"). There is
 *     no "Base SHA": the real projector (`_CONTEXT_FIELDS` in engine.py) never
 *     emits base_sha, so this feature does not render one.
 *   - GitHub anchors only when repository (owner/name) is known, or when evidence
 *     already carries a https://github.com/ URL. Never invent host/org/repo.
 */
describe("ControlPlaneView visible evidence — loop cards and detail", () => {
  beforeEach(() => mocked.mockReset());

  function bound(overrides: Record<string, unknown> = {}) {
    return state({
      runs: [
        { id: "swr-1", status: "running", goal: "Ship the adapter" },
        { id: "swr-2", status: "done", goal: "Fix the flake" },
      ],
      runId: "swr-1",
      snapshot: snapshot({
        context: {
          repository: "acme/widgets",
          branch: "feature/visible-evidence",
          head_sha: "deadbeef0123456789abcdef0123456789abcdef",
        },
        ...overrides,
      }),
    });
  }

  it("renders every engine run as a loop card showing goal and status (not only a select option)", () => {
    mocked.mockReturnValue(bound());
    render(<ControlPlaneView />);
    // queryAll* so missing cards fall through — getAll* would throw before the fallback.
    const namedItems = screen.queryAllByRole("listitem", {
      name: /Ship the adapter|Fix the flake/i,
    });
    const articles = screen.queryAllByRole("article");
    const cardSurfaces = namedItems.length >= 2 ? namedItems : articles;
    expect(cardSurfaces.length).toBeGreaterThanOrEqual(2);
    // Goals and statuses are visible on the card surface, not only inside a closed select.
    expect(screen.getByText("Ship the adapter")).toBeInTheDocument();
    expect(screen.getByText("Fix the flake")).toBeInTheDocument();
    expect(screen.getAllByText(/running/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/done/i).length).toBeGreaterThanOrEqual(1);
  });

  it("shows repository, branch, and current SHA in loop detail context (CP-004: no base SHA)", () => {
    mocked.mockReturnValue(bound());
    render(<ControlPlaneView />);

    // Labels: current — not the older "Head SHA" only wording.
    expect(screen.getByText("Current SHA")).toBeInTheDocument();
    expect(screen.getByText("Repository")).toBeInTheDocument();
    expect(screen.getByText("Branch")).toBeInTheDocument();
    // CP-004: the real projector never emits base_sha; this UI must not
    // render a "Base SHA" row at all.
    expect(screen.queryByText("Base SHA")).not.toBeInTheDocument();

    // Real values from the snapshot context (full or short form both acceptable for SHAs).
    expect(screen.getAllByText("acme/widgets").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("feature/visible-evidence").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/deadbeef0123/).length).toBeGreaterThanOrEqual(1);
  });

  it("surfaces repository and branch on the selected loop card (not only in the detail panel)", () => {
    mocked.mockReturnValue(bound());
    const { container } = render(<ControlPlaneView />);
    // Prefer an accessible card for the bound run; fall back to any article/listitem.
    const card =
      screen.queryByRole("listitem", { name: /Ship the adapter/i }) ??
      screen.queryByRole("article", { name: /Ship the adapter/i }) ??
      screen.queryAllByRole("listitem")[0] ??
      screen.queryAllByRole("article")[0];
    expect(card).toBeTruthy();
    // Card chrome must carry binding summary so the operator sees it without scrolling detail.
    expect(card!).toHaveTextContent("acme/widgets");
    expect(card!).toHaveTextContent("feature/visible-evidence");
    // Status on the card itself.
    expect(card!).toHaveTextContent(/running/i);
    // Guard: detail-only text elsewhere in the page is not enough if the card is empty of binding.
    void container;
  });

  it("does not render a base SHA row at all (CP-004: the projector never emits base_sha)", () => {
    mocked.mockReturnValue(
      bound({
        context: {
          repository: "acme/widgets",
          branch: "main",
          head_sha: "deadbeef0123456789abcdef0123456789abcdef",
        },
      }),
    );
    render(<ControlPlaneView />);
    expect(screen.queryByText("Base SHA")).not.toBeInTheDocument();
    // Must not invent a placeholder SHA that looks real.
    expect(screen.queryByText(/cafebabe/)).not.toBeInTheDocument();
  });

  it("makes repository, branch, and current SHA clickable GitHub links derived only from real context", () => {
    mocked.mockReturnValue(bound());
    render(<ControlPlaneView />);

    const repoLink = screen.getByRole("link", { name: "acme/widgets" });
    expect(repoLink).toHaveAttribute("href", "https://github.com/acme/widgets");

    const branchLink = screen.getByRole("link", { name: "feature/visible-evidence" });
    expect(branchLink).toHaveAttribute(
      "href",
      "https://github.com/acme/widgets/tree/feature/visible-evidence",
    );

    const shaLink = screen.getByRole("link", {
      name: /deadbeef0123456789abcdef0123456789abcdef|deadbeef0123/,
    });
    expect(shaLink).toHaveAttribute(
      "href",
      "https://github.com/acme/widgets/commit/deadbeef0123456789abcdef0123456789abcdef",
    );
  });

  it("does not invent repository/branch/commit links when repository context is null", () => {
    mocked.mockReturnValue(
      bound({
        context: {
          repository: null,
          branch: "feature/orphan",
          head_sha: "deadbeef0123456789abcdef0123456789abcdef",
        },
      }),
    );
    render(<ControlPlaneView />);
    // Values may still show as text, but must not become navigable GitHub anchors.
    expect(screen.queryByRole("link", { name: "feature/orphan" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /deadbeef0123/ })).not.toBeInTheDocument();
    // No invented org/repo host.
    expect(screen.queryByRole("link", { name: /github\.com/i })).not.toBeInTheDocument();
  });

  it("renders a clickable GitHub PR link only when evidence supplies a github.com PR URL", () => {
    mocked.mockReturnValue(
      bound({
        evidence: {
          commits: [],
          files: [],
          tests: [],
          reports: [
            {
              name: "PR #42",
              status: "open",
              url: "https://github.com/acme/widgets/pull/42",
            },
            {
              name: "external review",
              status: "done",
              url: "https://evil.example.com/pull/99",
            },
          ],
        },
      }),
    );
    render(<ControlPlaneView />);
    const prLink = screen.getByRole("link", { name: /PR #42/ });
    expect(prLink).toHaveAttribute("href", "https://github.com/acme/widgets/pull/42");
    expect(screen.queryByRole("link", { name: /external review/ })).not.toBeInTheDocument();
    expect(screen.getByText("external review")).toBeInTheDocument();
  });

  it("derives a commit link from repository + sha when the evidence entry url is null", () => {
    mocked.mockReturnValue(
      bound({
        evidence: {
          commits: [
            { sha: "aaa1111bbbb2222cccc3333dddd4444eeee5555", message: "wire evidence", url: null },
          ],
          files: [],
          tests: [],
          reports: [],
        },
      }),
    );
    render(<ControlPlaneView />);
    const commitLink = screen.getByRole("link", {
      name: /aaa1111bbbb2222cccc3333dddd4444eeee5555|aaa1111/,
    });
    expect(commitLink).toHaveAttribute(
      "href",
      "https://github.com/acme/widgets/commit/aaa1111bbbb2222cccc3333dddd4444eeee5555",
    );
  });

  it("keeps a backend-refused non-GitHub commit URL inert and never fabricates a github.com link (CP-003)", () => {
    mocked.mockReturnValue(
      bound({
        evidence: {
          // engine.py maps an explicit https://gitlab... URL to null and flags
          // url_refused -- distinct from a reporter never supplying a url.
          commits: [
            {
              sha: "aaa1111bbbb2222cccc3333dddd4444eeee5555",
              message: "refused link",
              url: null,
              url_refused: true,
            },
          ],
          files: [],
          tests: [],
          reports: [],
        },
      }),
    );
    render(<ControlPlaneView />);
    // Once fixed, refusal is distinguishable from absence and cannot become a link.
    expect(
      screen.queryByRole("link", { name: /aaa1111bbbb2222cccc3333dddd4444eeee5555|aaa1111/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/aaa1111/)).toBeInTheDocument();
  });

  it("does not invent a commit link when repository is null and evidence url is null", () => {
    mocked.mockReturnValue(
      bound({
        context: {
          repository: null,
          branch: "main",
          head_sha: "deadbeef0123456789abcdef0123456789abcdef",
        },
        evidence: {
          commits: [
            { sha: "aaa1111bbbb2222cccc3333dddd4444eeee5555", message: "orphan commit", url: null },
          ],
          files: [],
          tests: [],
          reports: [],
        },
      }),
    );
    render(<ControlPlaneView />);
    expect(
      screen.queryByRole("link", { name: /aaa1111bbbb2222cccc3333dddd4444eeee5555|aaa1111/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/aaa1111/)).toBeInTheDocument();
  });

  it("renders an explicit verification state from evidence.tests without implying approval", () => {
    mocked.mockReturnValue(
      bound({
        evidence: {
          commits: [],
          files: [],
          tests: [
            { name: "test_a", status: "passed" },
            { name: "test_b", status: "passed" },
          ],
          reports: [],
        },
        approval: { approved: false, receipt: null },
      }),
    );
    render(<ControlPlaneView />);
    // Dedicated status region — incidental test-row "passed" text must not satisfy this.
    const band = screen.getByRole("status", { name: /verification/i });
    expect(band).toHaveTextContent(/passed|all passed|tests passed/i);
    // Passing tests still must not flip approval.
    expect(screen.getByText("No approval recorded")).toBeInTheDocument();
  });

  it("marks verification failed when any reported test failed", () => {
    mocked.mockReturnValue(
      bound({
        evidence: {
          commits: [],
          files: [],
          tests: [
            { name: "test_a", status: "passed" },
            { name: "test_b", status: "failed" },
          ],
          reports: [],
        },
      }),
    );
    render(<ControlPlaneView />);
    const band = screen.getByRole("status", { name: /verification/i });
    expect(band).toHaveTextContent(/failed/i);
  });

  it("marks verification unknown when no tests have been reported", () => {
    mocked.mockReturnValue(
      bound({
        evidence: { commits: [], files: [], tests: [], reports: [] },
      }),
    );
    render(<ControlPlaneView />);
    const band = screen.getByRole("status", { name: /verification/i });
    // Honest empty inside the band — not a green "passed".
    expect(band).toHaveTextContent(/unknown|no tests|not reported/i);
    expect(band).not.toHaveTextContent(/passed/i);
  });

  it("keeps agent conversation visible as inert text on the detail surface", () => {
    mocked.mockReturnValue(
      bound({
        activity: [
          {
            id: 7,
            action: "agent.message",
            created_at: "2026-08-12T12:00:00Z",
            payload: { note: "working on visible evidence" },
          },
        ],
      }),
    );
    render(<ControlPlaneView />);
    expect(screen.getByText("agent.message")).toBeInTheDocument();
    // The raw payload (including note) is collapsed by default (F004
    // landed on main) -- open the disclosure to reach it, still as inert
    // text, never an anchor or executable content.
    expect(screen.queryByText("working on visible evidence")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText(/raw payload/i));
    expect(screen.getByText("working on visible evidence")).toBeInTheDocument();
  });

  // CP-002: the run card AND the detail panel must both bind strictly to the
  // currently selected runId -- never render the previous run's context
  // during the fetch window after a new selection.
  it("does not render the prior run's context under a newly selected run (loop card)", () => {
    mocked.mockReturnValue(
      state({
        runs: [
          { id: "swr-1", status: "running", goal: "Old run" },
          { id: "swr-2", status: "running", goal: "New run" },
        ],
        // This state exists after setSelectedRunId and before the new fetch settles.
        runId: "swr-2",
        snapshot: snapshot({
          run: { id: "swr-1", status: "running", goal: "Old run" },
          context: { repository: "old/repo", branch: "old-branch", head_sha: "deadbeef" },
        }),
      }),
    );

    render(<ControlPlaneView />);
    const selectedCard = screen.getByRole("listitem", { name: "New run" });
    expect(selectedCard).not.toHaveTextContent(/old\/repo|old-branch/);
    // The detail sibling must obey the same binding check.
    expect(screen.queryAllByText("old/repo")).toHaveLength(0);
    expect(screen.queryAllByText("old-branch")).toHaveLength(0);
  });

  it("shows a loading indicator on the selected card while its snapshot is still in flight, never stale context", () => {
    mocked.mockReturnValue(
      state({
        runs: [
          { id: "swr-1", status: "running", goal: "Old run" },
          { id: "swr-2", status: "running", goal: "New run" },
        ],
        runId: "swr-2",
        loading: true,
        snapshot: snapshot({
          run: { id: "swr-1", status: "running", goal: "Old run" },
          context: { repository: "old/repo", branch: "old-branch", head_sha: "deadbeef" },
        }),
      }),
    );

    render(<ControlPlaneView />);
    const selectedCard = screen.getByRole("listitem", { name: "New run" });
    expect(selectedCard).toHaveTextContent(/loading/i);
    expect(selectedCard).not.toHaveTextContent(/old\/repo/);
  });

  it("does not render the detail panel from a snapshot bound to a different run", () => {
    mocked.mockReturnValue(
      state({
        runs: [
          { id: "swr-1", status: "running", goal: "Old run" },
          { id: "swr-2", status: "running", goal: "New run" },
        ],
        runId: "swr-2",
        snapshot: snapshot({
          run: { id: "swr-1", status: "running", goal: "Old run" },
          context: { repository: "old/repo", branch: "old-branch", head_sha: "deadbeef" },
          evidence: {
            commits: [{ sha: "aaa1111", message: "old commit" }],
            files: [],
            tests: [],
            reports: [],
          },
        }),
      }),
    );

    render(<ControlPlaneView />);
    // The stale evidence/context must not leak into the detail surface.
    expect(screen.queryByText("old commit")).not.toBeInTheDocument();
    expect(screen.queryByText("old-branch")).not.toBeInTheDocument();
    // A loading/stale indicator is shown instead.
    expect(screen.getByRole("status", { name: /loading run evidence/i })).toBeInTheDocument();
  });

  it("renders the detail panel once the snapshot is bound to the selected run", () => {
    mocked.mockReturnValue(
      state({
        runs: [{ id: "swr-2", status: "running", goal: "New run" }],
        runId: "swr-2",
        snapshot: snapshot({
          run: { id: "swr-2", status: "running", goal: "New run" },
          context: { repository: "new/repo", branch: "new-branch", head_sha: "cafebabe" },
        }),
      }),
    );

    render(<ControlPlaneView />);
    expect(screen.getAllByText("new/repo").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByRole("status", { name: /loading run evidence/i })).not.toBeInTheDocument();
  });
});
