import { describe, expect, it } from "vitest";
import { mergeEngineRunSnapshot } from "./hooks";

function snapshot(
  runId: string,
  cursor: number,
  ids: number[],
  evidence: {
    commits?: Array<{ sha: string; url_refused?: boolean }>;
    files?: Array<{ path: string }>;
    tests?: Array<{ name: string; status?: string | null; url_refused?: boolean }>;
  } = {},
) {
  return {
    api_version: "loopdeck-engine/v1",
    run: { id: runId, status: "running" },
    tasks: [],
    deps: [],
    attempts: {},
    progress: {},
    metrics: {},
    artifacts: [],
    activity: ids.map((id) => ({ id, action: `event-${id}`, payload: {} })),
    next_activity_cursor: cursor,
    context: { repository: null, branch: null, head_sha: null },
    evidence: {
      commits: evidence.commits ?? [],
      files: evidence.files ?? [],
      tests: evidence.tests ?? [],
      reports: [],
    },
    approval: { approved: false, receipt: null },
  };
}

describe("bound engine run cursor merge", () => {
  it("deduplicates cursor overlap and keeps ordered confirmed activity", () => {
    const merged = mergeEngineRunSnapshot(snapshot("swr-1", 2, [1, 2]), snapshot("swr-1", 3, [2, 3]));
    expect(merged.activity.map((event) => event.id)).toEqual([1, 2, 3]);
    expect(merged.next_activity_cursor).toBe(3);
  });

  it("does not carry activity across authoritative run bindings", () => {
    const merged = mergeEngineRunSnapshot(snapshot("swr-old", 8, [8]), snapshot("swr-new", 1, [1]));
    expect(merged.activity.map((event) => event.id)).toEqual([1]);
  });

  it("accumulates evidence across polls for the same run, deduplicated by identity", () => {
    const first = snapshot("swr-1", 1, [1], { commits: [{ sha: "aaa1111" }] });
    const second = snapshot("swr-1", 2, [2], {
      commits: [{ sha: "aaa1111" }, { sha: "bbb2222" }],
      files: [{ path: "a.py" }],
    });
    const merged = mergeEngineRunSnapshot(first, second);
    expect(merged.evidence.commits.map((c) => c.sha)).toEqual(["aaa1111", "bbb2222"]);
    expect(merged.evidence.files.map((f) => f.path)).toEqual(["a.py"]);
  });

  it("lets a later refused url win the commit dedup over an earlier absent url (CP-003 r2)", () => {
    const first = snapshot("swr-1", 1, [1], { commits: [{ sha: "aaa1111" }] });
    const second = snapshot("swr-1", 2, [2], {
      commits: [{ sha: "aaa1111", url_refused: true }],
    });
    const merged = mergeEngineRunSnapshot(first, second);
    expect(merged.evidence.commits).toHaveLength(1);
    expect(
      (merged.evidence.commits[0] as { url_refused?: boolean }).url_refused,
    ).toBe(true);
  });

  it("resets accumulated evidence when the authoritative run binding changes", () => {
    const old = snapshot("swr-old", 8, [8], { commits: [{ sha: "aaa1111" }] });
    const fresh = snapshot("swr-new", 1, [1], { commits: [{ sha: "bbb2222" }] });
    const merged = mergeEngineRunSnapshot(old, fresh);
    expect(merged.evidence.commits.map((c) => c.sha)).toEqual(["bbb2222"]);
  });

  it("does not resurrect a stale run's activity when a null previous snapshot is passed (first poll)", () => {
    const merged = mergeEngineRunSnapshot(null, snapshot("swr-1", 1, [1]));
    expect(merged.activity.map((event) => event.id)).toEqual([1]);
    expect(merged.evidence.commits).toEqual([]);
  });

  // CP-001
  it("lets a later failed test repair an earlier passed test", () => {
    const first = snapshot("swr-1", 1, [1], { tests: [{ name: "test_a", status: "passed" }] });
    const second = snapshot("swr-1", 2, [2], { tests: [{ name: "test_a", status: "failed" }] });

    const merged = mergeEngineRunSnapshot(first, second);

    // Once fixed, VerificationBand receives "failed" and cannot render "all passed".
    expect(merged.evidence.tests[0]?.status).toBe("failed");
  });

  it("keeps an already-failed test failed when a later poll re-reports it as passed", () => {
    const first = snapshot("swr-1", 1, [1], { tests: [{ name: "test_a", status: "failed" }] });
    const second = snapshot("swr-1", 2, [2], { tests: [{ name: "test_a", status: "passed" }] });

    const merged = mergeEngineRunSnapshot(first, second);

    expect(merged.evidence.tests[0]?.status).toBe("failed");
  });

  it("does not let a differently-named test's adverse status leak onto another identity", () => {
    const first = snapshot("swr-1", 1, [1], {
      tests: [
        { name: "test_a", status: "passed" },
        { name: "test_b", status: "passed" },
      ],
    });
    const second = snapshot("swr-1", 2, [2], { tests: [{ name: "test_a", status: "failed" }] });

    const merged = mergeEngineRunSnapshot(first, second);

    expect(merged.evidence.tests.find((t) => t.name === "test_a")?.status).toBe("failed");
    expect(merged.evidence.tests.find((t) => t.name === "test_b")?.status).toBe("passed");
  });

  // Round-3 precedence matrix: adverse status and url_refused merge
  // FIELD-WISE, never via whole-entry replacement. Both orders must hold.
  it("keeps an earlier failed status AND picks up a later url_refused (status-then-refused order)", () => {
    const first = snapshot("swr-1", 1, [1], { tests: [{ name: "test_a", status: "failed" }] });
    const second = snapshot("swr-1", 2, [2], {
      tests: [{ name: "test_a", status: "passed", url_refused: true }],
    });

    const merged = mergeEngineRunSnapshot(first, second);
    const test = merged.evidence.tests[0] as { status?: string | null; url_refused?: boolean };

    expect(test.status).toBe("failed");
    expect(test.url_refused).toBe(true);
  });

  it("keeps an earlier url_refused AND picks up a later adverse status (refused-then-status order)", () => {
    const first = snapshot("swr-1", 1, [1], {
      tests: [{ name: "test_a", status: "passed", url_refused: true }],
    });
    const second = snapshot("swr-1", 2, [2], { tests: [{ name: "test_a", status: "failed" }] });

    const merged = mergeEngineRunSnapshot(first, second);
    const test = merged.evidence.tests[0] as { status?: string | null; url_refused?: boolean };

    expect(test.status).toBe("failed");
    expect(test.url_refused).toBe(true);
  });
});
