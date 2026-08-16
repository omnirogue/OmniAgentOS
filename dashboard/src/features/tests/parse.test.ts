import { describe, expect, it } from "vitest";
import { classifyRun, parsePytestSummary, type ClassifyRunInput } from "./parse";

describe("parsePytestSummary", () => {
  it("parses a standard summary with every count token", () => {
    const parsed = parsePytestSummary(
      "1 failed, 1204 passed, 1 skipped, 1 warning in 591.90s (0:09:51)",
    );
    expect(parsed).toEqual({
      failed: 1,
      passed: 1204,
      skipped: 1,
      warnings: 1,
      deselected: null,
      durationSeconds: 591.9,
    });
  });

  it("leaves absent failed/skipped tokens as null, not 0", () => {
    const parsed = parsePytestSummary("1204 passed in 12.3s");
    expect(parsed).toEqual({
      failed: null,
      passed: 1204,
      skipped: null,
      warnings: null,
      deselected: null,
      durationSeconds: 12.3,
    });
  });

  it("keeps an explicit-zero failed token as 0", () => {
    const parsed = parsePytestSummary("0 failed, 1204 passed in 12.3s");
    expect(parsed).toEqual({
      failed: 0,
      passed: 1204,
      skipped: null,
      warnings: null,
      deselected: null,
      durationSeconds: 12.3,
    });
  });

  it("returns null for the counterfeit-counter grammar", () => {
    expect(parsePytestSummary("total=107 caught=107 survived=0")).toBeNull();
  });

  it("returns null for a ruff-delta detail", () => {
    expect(parsePytestSummary("0 -> 0")).toBeNull();
  });

  it("returns null for an empty string", () => {
    expect(parsePytestSummary("")).toBeNull();
  });

  it("returns null for whitespace-only and free-text details", () => {
    expect(parsePytestSummary("   ")).toBeNull();
    expect(parsePytestSummary("/Users/youruser/OmniAgentOS-gate/.venv/bin/python")).toBeNull();
  });

  it("does not throw on a non-string", () => {
    expect(parsePytestSummary(undefined as unknown as string)).toBeNull();
    expect(parsePytestSummary(null as unknown as string)).toBeNull();
  });
});

function baseRun(overrides: Partial<ClassifyRunInput> = {}): ClassifyRunInput {
  return {
    exit_code: 1,
    instrument_error: null,
    refusal_reason: "",
    steps: [],
    ...overrides,
  };
}

describe("classifyRun", () => {
  it("does not throw on a 22-key-old-shape object missing instrument_error", () => {
    const oldShape = {
      exit_code: 2,
      refusal_reason:
        "unpinned-workspace: REPO=/Users/youruser/OmniAgentOS is not the pinned gate workspace",
      steps: [{ name: "workspace-pin", status: "failed" }],
    };
    expect(() => classifyRun(oldShape as ClassifyRunInput)).not.toThrow();
    expect(classifyRun(oldShape as ClassifyRunInput)).toBe("mechanical_refusal");
  });

  it("is unaffected when mode is absent (mode is display-only)", () => {
    const withoutMode = baseRun({
      exit_code: 1,
      refusal_reason: "scripts: 1 failed, 1204 passed in 12.3s",
      steps: [{ name: "scripts", status: "failed" }],
    });
    const withMode = { ...withoutMode, mode: "full" };
    expect(classifyRun(withoutMode)).toBe("candidate_defect");
    expect(classifyRun(withMode as ClassifyRunInput)).toBe("candidate_defect");
  });

  it("returns unclassified for an unrecognized refusal string", () => {
    expect(
      classifyRun(
        baseRun({
          exit_code: 1,
          instrument_error: null,
          refusal_reason: "gremlins ate the run",
          steps: [{ name: "ladder", status: "ok" }],
        }),
      ),
    ).toBe("unclassified");
  });

  it("lets pass win over a non-null instrument_error (rule 1 before rule 2)", () => {
    // Both DESIGN.md §1 and this package's brief list exit_code === 0 first.
    // No live receipt in the 784-file store has exit_code 0 AND a set
    // instrument_error (0 of 76 instrument_error rows are exit 0), so this
    // is a priority-order test, not a replay of a real file.
    expect(
      classifyRun(
        baseRun({
          exit_code: 0,
          instrument_error: "mint stalled mid-write",
          refusal_reason: "",
          steps: [],
        }),
      ),
    ).toBe("pass");
  });

  it("classifies a non-empty instrument_error when exit_code is nonzero", () => {
    expect(
      classifyRun(
        baseRun({
          exit_code: 70,
          instrument_error: "true",
          refusal_reason: "gate exited without an explicit verdict (abnormal termination)",
          steps: [],
        }),
      ),
    ).toBe("instrument_error");
  });

  it("treats missing/empty instrument_error as absent, not a match", () => {
    expect(
      classifyRun(
        baseRun({
          exit_code: 1,
          instrument_error: "",
          refusal_reason: "gremlins ate the run",
          steps: [],
        }),
      ),
    ).toBe("unclassified");
    expect(
      classifyRun(
        baseRun({
          exit_code: 1,
          instrument_error: undefined,
          refusal_reason: "gremlins ate the run",
          steps: [],
        }),
      ),
    ).toBe("unclassified");
  });

  it("classifies a colon-prefixed suite failure as candidate_defect", () => {
    expect(
      classifyRun(
        baseRun({
          exit_code: 1,
          refusal_reason: "scripts: 1 failed, 1204 passed, 1 skipped, 1 warning in 591.90s",
          steps: [{ name: "scripts", status: "failed" }],
        }),
      ),
    ).toBe("candidate_defect");
  });

  it("classifies a mechanical taxonomy string even when a step also failed", () => {
    expect(
      classifyRun(
        baseRun({
          exit_code: 2,
          refusal_reason: "dirty-workspace: uncommitted files in the gate workspace",
          steps: [{ name: "workspace-clean", status: "failed" }],
        }),
      ),
    ).toBe("mechanical_refusal");
  });
});
