import { describe, expect, it } from "vitest";
import { gateSummary, gateTypeLabel } from "./format";
import { GATE_TYPES, type GateType, type Routine } from "./types";

/**
 * Contract parity: the backend accepts FOUR gate types — the frontend union
 * must match, key for key.
 *
 * `merge_candidate` landed server-side in migration 091
 * (omniagentos/db/migrations/091_routines_merge_candidate_gate.sql) and is in
 * omniagentos/scheduler/routines.py's GATE_TYPES and
 * routines_settle.py's EXECUTED_GATE_TYPES. Before this fixture existed, the
 * dashboard union stopped at three values, so a legitimate API row rendered
 * `gateTypeLabel(...) === undefined` and the create page could not build one.
 *
 * The fixture below is shaped exactly like an API routine row for a
 * merge_candidate gate: gate_config carries command + expected_exit_code 0
 * (objective-verifier rule) plus candidate_sha and merge_base_sha, both
 * 40-char lowercase git SHAs (routines.py `_GIT_SHA_RE`).
 */
const MERGE_CANDIDATE_ROW: Routine = {
  id: "rtn_merge_gate",
  name: "lane-merge-gate",
  description: "grades a candidate sha for merge to main",
  trigger_type: "event",
  trigger_config: { event: "lane.candidate" },
  task_template: { title: "Gate a merge candidate" },
  gate_type: "merge_candidate",
  gate_config: {
    command: "uv run pytest tests -q",
    expected_exit_code: 0,
    candidate_sha: "9c4b6f0d2e8a1b3c5d7e9f0a2b4c6d8e0f1a3b5c",
    merge_base_sha: "f75f7395ec67bc00bde25558c76c79692f528175",
  },
  hard_cap_type: "max_iterations",
  hard_cap_value: 5,
  notification_target: { channel: "slack" },
  status: "active",
  auto_pause_reason: "",
  total_runs: 2,
  accepted_runs: 1,
  neutral_runs: 0,
  acceptance_rate: 0.5,
  total_cost_usd: null,
  cost_per_accepted_change: null,
  last_fired: null,
  created_at: "2026-08-08T00:00:00Z",
  updated_at: "2026-08-08T00:00:00Z",
};

describe("gate-type contract parity with the backend", () => {
  it("GATE_TYPES carries every backend-accepted gate type, merge_candidate included", () => {
    expect(GATE_TYPES).toEqual([
      "exit_code",
      "test_command",
      "metric_threshold",
      "merge_candidate",
    ]);
  });

  it("gateTypeLabel yields a human label — never undefined — for a merge_candidate API row", () => {
    expect(gateTypeLabel(MERGE_CANDIDATE_ROW.gate_type)).toBe("Merge candidate");
  });

  it("gateTypeLabel still covers every member of GATE_TYPES (exhaustiveness guard)", () => {
    for (const type of GATE_TYPES) {
      const label = gateTypeLabel(type);
      expect(label, `label for ${type}`).toBeTypeOf("string");
      expect(label.length).toBeGreaterThan(0);
    }
  });

  it("gateSummary shows the merge_candidate gate's verifier command and both SHAs", () => {
    expect(gateSummary(MERGE_CANDIDATE_ROW.gate_type, MERGE_CANDIDATE_ROW.gate_config)).toBe(
      "uv run pytest tests -q @ 9c4b6f0d2e8a (base f75f7395ec67)",
    );
  });

  it("merge_candidate is assignable to GateType (compile-time pin)", () => {
    const value: GateType = "merge_candidate";
    expect(value).toBe("merge_candidate");
  });
});
