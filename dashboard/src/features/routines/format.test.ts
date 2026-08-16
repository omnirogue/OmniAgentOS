import { describe, expect, it } from "vitest";
import { costPerAcceptedChangeLabel, gateSummary, gateTypeLabel } from "./format";
import type { Routine } from "./types";

/**
 * ISSUE-8 — "we don't know" must never render as "$0.00".
 *
 * `cost_per_accepted_change` is `total_cost_usd / accepted_runs`
 * (omniagentos/scheduler/store.py). Before migration 119, `total_cost_usd`
 * was `NOT NULL DEFAULT 0`, so a routine whose runs never had a reported
 * cost computed an honest-looking `0 / accepted_runs = 0.0` — a real
 * number, not `null` — and this label rendered it as "$0.00", indistinguishable
 * from a routine that genuinely cost nothing. Migration 119 lets
 * `total_cost_usd` (and therefore this ratio) be NULL when at least one
 * contributing run's cost was never reported; this label must say so.
 */

function baseRoutine(overrides: Partial<Routine> = {}): Routine {
  return {
    id: "rtn_1",
    name: "some-routine",
    description: "",
    trigger_type: "cron",
    trigger_config: {},
    task_template: {},
    gate_type: "exit_code",
    gate_config: {},
    hard_cap_type: "budget_usd",
    hard_cap_value: 1,
    notification_target: {},
    status: "active",
    auto_pause_reason: "",
    total_runs: 4,
    accepted_runs: 4,
    acceptance_rate: 1,
    total_cost_usd: 0,
    cost_per_accepted_change: null,
    last_fired: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("costPerAcceptedChangeLabel", () => {
  it('renders "unknown" — never $0.00 — when cost_per_accepted_change is null', () => {
    const routine = baseRoutine({ cost_per_accepted_change: null });
    expect(costPerAcceptedChangeLabel(routine)).toBe("unknown");
  });

  it("still renders $0.00 for a genuinely, exactly zero cost", () => {
    const routine = baseRoutine({ cost_per_accepted_change: 0 });
    expect(costPerAcceptedChangeLabel(routine)).toBe("$0.00");
  });

  it("renders an ordinary known positive cost as before (regression guard)", () => {
    const routine = baseRoutine({ cost_per_accepted_change: 1.25 });
    expect(costPerAcceptedChangeLabel(routine)).toBe("$1.25");
  });

  it("renders a small known cost that rounds toward zero, but is not exactly zero, as a dollar figure", () => {
    const routine = baseRoutine({ cost_per_accepted_change: 0.001 });
    expect(costPerAcceptedChangeLabel(routine)).toBe("$0.00");
  });
});

/**
 * Dashboard contract parity — the API accepts gate_type "merge_candidate"
 * (omniagentos/scheduler/routines.py:31, migration 091), but GATE_LABELS
 * previously had no entry for it, so `gateTypeLabel("merge_candidate")`
 * returned `undefined` for any routine row the API sent with that gate
 * type. This must return a real, human-readable label instead.
 */
describe("gateTypeLabel", () => {
  it('returns a real label for "merge_candidate", never undefined', () => {
    expect(gateTypeLabel("merge_candidate")).toBe("Merge candidate");
  });

  it("still returns the pre-existing labels (regression guard)", () => {
    expect(gateTypeLabel("exit_code")).toBe("Exit code");
    expect(gateTypeLabel("test_command")).toBe("Test command");
    expect(gateTypeLabel("metric_threshold")).toBe("Metric threshold");
  });
});

/**
 * CR-001 — the API validates `merge_base_sha` exactly as strictly as
 * `candidate_sha` (a 40-char lowercase git SHA), so a routine row whose
 * merge_base_sha is missing or empty is broken, not healthy. Before this
 * fix, `gateSummary` never read `merge_base_sha` at all, so a broken row
 * rendered byte-identical to a healthy one — no way for anyone reading the
 * routines list to tell them apart.
 */
describe("gateSummary — merge_candidate", () => {
  const healthyConfig = {
    command: "pytest tests/routines",
    candidate_sha: "a".repeat(40),
    merge_base_sha: "b".repeat(40),
  };

  it("surfaces both the candidate SHA and the merge-base SHA for a healthy row", () => {
    const summary = gateSummary("merge_candidate", healthyConfig);
    expect(summary).toContain("aaaaaaaaaaaa");
    expect(summary).toContain("bbbbbbbbbbbb");
  });

  it("renders an explicit marker — never a healthy-looking value — when merge_base_sha is missing", () => {
    const config = { ...healthyConfig };
    delete (config as Record<string, unknown>).merge_base_sha;
    const summary = gateSummary("merge_candidate", config);
    expect(summary).not.toBe(gateSummary("merge_candidate", healthyConfig));
    expect(summary).toMatch(/unknown/i);
  });

  it("renders an explicit marker — never a healthy-looking value — when merge_base_sha is empty", () => {
    const config = { ...healthyConfig, merge_base_sha: "" };
    const summary = gateSummary("merge_candidate", config);
    expect(summary).not.toBe(gateSummary("merge_candidate", healthyConfig));
    expect(summary).toMatch(/unknown/i);
  });
});
