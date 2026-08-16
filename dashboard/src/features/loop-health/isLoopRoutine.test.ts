import { describe, expect, it } from "vitest";
import type { Routine } from "@/features/routines/types";
import { isLoopRoutine, loopInstanceId } from "./isLoopRoutine";

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
    total_runs: 0,
    accepted_runs: 0,
    acceptance_rate: null,
    total_cost_usd: 0,
    cost_per_accepted_change: null,
    last_fired: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("isLoopRoutine", () => {
  it("is true when task_template.input.instance_module is a non-empty string", () => {
    const routine = baseRoutine({
      task_template: { input: { instance_module: "omniagentos_loops.instances.health_monitor" } },
    });
    expect(isLoopRoutine(routine)).toBe(true);
  });

  it("is false for an ordinary (non-loop) routine", () => {
    const routine = baseRoutine({ task_template: { input: { kind: "skill", skill: "ops.heartbeat" } } });
    expect(isLoopRoutine(routine)).toBe(false);
  });

  it("is false when task_template is empty/malformed", () => {
    expect(isLoopRoutine(baseRoutine({ task_template: {} }))).toBe(false);
    expect(isLoopRoutine(baseRoutine({ task_template: { input: {} } }))).toBe(false);
    expect(isLoopRoutine(baseRoutine({ task_template: { input: { instance_module: "" } } }))).toBe(false);
  });
});

describe("loopInstanceId", () => {
  it("reads task_template.input.instance_id", () => {
    const routine = baseRoutine({
      task_template: { input: { instance_id: "w3_health_monitor", instance_module: "x" } },
    });
    expect(loopInstanceId(routine)).toBe("w3_health_monitor");
  });

  it("falls back to the routine id when instance_id is absent", () => {
    const routine = baseRoutine({ id: "rtn_fallback", task_template: {} });
    expect(loopInstanceId(routine)).toBe("rtn_fallback");
  });
});
