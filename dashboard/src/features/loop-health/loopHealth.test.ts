import { describe, expect, it } from "vitest";
import type { Approval } from "@/lib/contracts";
import type { RecentRunItem, Routine } from "@/features/routines/types";
import {
  classifyTickOutcome,
  cronIntervalMs,
  deriveLoopHealth,
  formatDuration,
} from "./loopHealth";

const NOW = new Date("2026-08-01T20:00:00Z");

function loopRoutine(overrides: Partial<Routine> = {}): Routine {
  return {
    id: "rtn_health",
    name: "w3-health-monitor",
    description: "",
    trigger_type: "cron",
    trigger_config: { cron: "*/10 * * * *" },
    task_template: {
      input: {
        module: "omniagentos.loops",
        template: "monitor_diagnose_repair_verify",
        instance_id: "w3_health_monitor",
        instance_module: "omniagentos_loops.instances.health_monitor",
      },
    },
    gate_type: "exit_code",
    gate_config: {},
    hard_cap_type: "budget_usd",
    hard_cap_value: 5,
    notification_target: {},
    status: "active",
    auto_pause_reason: "",
    total_runs: 2,
    accepted_runs: 0,
    acceptance_rate: null,
    total_cost_usd: 0.5,
    cost_per_accepted_change: null,
    last_fired: "2026-08-01T19:55:00Z", // 5m before NOW
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-08-01T19:55:00Z",
    ...overrides,
  };
}

function nonLoopRoutine(overrides: Partial<Routine> = {}): Routine {
  return {
    ...loopRoutine(),
    id: "rtn_ordinary",
    name: "ordinary-sweep",
    task_template: { input: { kind: "skill", skill: "ops.heartbeat" } },
    ...overrides,
  };
}

function run(overrides: Partial<RecentRunItem> = {}): RecentRunItem {
  return {
    routine_id: "rtn_health",
    routine_name: "w3-health-monitor",
    run_id: "run_1",
    gate_passed: null,
    accepted: null,
    cost_usd: 0,
    finished_at: "2026-08-01T19:55:00Z",
    ...overrides,
  };
}

function approval(overrides: Partial<Approval> = {}): Approval {
  return {
    id: "apr_1",
    run_id: null,
    task_id: null,
    step_seq: null,
    action_class: "consequential",
    proposed_action: "w3_health_monitor/diagnose: restart service",
    risk: "loop_approval",
    evidence: "loop effect at or above the T2 approval floor",
    state: "pending",
    decided_by: null,
    decision_note: null,
    decided_at: null,
    expires_at: "2026-08-02T00:00:00Z",
    created_at: "2026-08-01T19:11:46Z",
    ...overrides,
  };
}

describe("classifyTickOutcome", () => {
  it("is pending while finished_at is null (still running)", () => {
    expect(classifyTickOutcome(run({ finished_at: null }))).toBe("pending");
  });

  it("is neutral when a settled run carries no verdict (the common parked/idle shape)", () => {
    expect(classifyTickOutcome(run({ gate_passed: null, accepted: null }))).toBe("neutral");
  });

  it("is favourable when accepted is true", () => {
    expect(classifyTickOutcome(run({ gate_passed: true, accepted: true }))).toBe("favourable");
  });

  it("is adverse when a verdict exists and accepted is not true", () => {
    expect(classifyTickOutcome(run({ gate_passed: false, accepted: false }))).toBe("adverse");
  });
});

describe("cronIntervalMs", () => {
  it("recovers the true cadence of a minute-stepped cron regardless of phase", () => {
    expect(cronIntervalMs("*/10 * * * *", NOW)).toBe(10 * 60_000);
  });

  it("recovers a 24h cadence for a daily cron even measured off-phase", () => {
    // NOW is 20:00Z; "0 3 * * *" fires once a day at 03:00 -- the gap
    // between two consecutive fires must still read as ~24h, not the ~7h
    // until the next one.
    expect(cronIntervalMs("0 3 * * *", NOW)).toBe(24 * 60 * 60_000);
  });

  it("returns null for an unparseable cron (no verdict fabricated)", () => {
    expect(cronIntervalMs("not a cron", NOW)).toBeNull();
  });
});

describe("formatDuration", () => {
  it("renders minutes, hours, and days without seconds", () => {
    expect(formatDuration(5 * 60_000)).toBe("5m");
    expect(formatDuration(90 * 60_000)).toBe("1h 30m");
    expect(formatDuration(50 * 60 * 60_000)).toBe("2d 2h");
  });
});

describe("deriveLoopHealth", () => {
  it("only includes routines carrying task_template.input.instance_module", () => {
    const result = deriveLoopHealth([loopRoutine(), nonLoopRoutine()], [], [], NOW);
    expect(result.loops).toHaveLength(1);
    expect(result.loops[0]!.routine.id).toBe("rtn_health");
  });

  it("STALE: a routine whose last run is older than 2x its cron interval renders stale, never its last known status", () => {
    const routine = loopRoutine({
      last_fired: new Date(NOW.getTime() - 25 * 60_000).toISOString(), // 25m ago, cron */10 -> stale past 20m
    });
    const result = deriveLoopHealth([routine], [], [], NOW);
    const row = result.loops[0]!;
    expect(row.state).toBe("stale");
    expect(row.tone).toBe("danger");
    expect(row.isStale).toBe(true);
    expect(row.stalenessText).toMatch(/^STALE/);
  });

  it("a routine ticking within its cadence is OK, not stale", () => {
    const routine = loopRoutine({
      last_fired: new Date(NOW.getTime() - 4 * 60_000).toISOString(), // 4m ago
    });
    const result = deriveLoopHealth([routine], [], [], NOW);
    const row = result.loops[0]!;
    expect(row.state).toBe("ok");
    expect(row.isStale).toBe(false);
  });

  it("an unparseable cron emits no overdue verdict, even with a very old last_fired", () => {
    const routine = loopRoutine({
      trigger_config: { cron: "not-a-cron" },
      last_fired: new Date(NOW.getTime() - 999 * 60_000).toISOString(),
    });
    const result = deriveLoopHealth([routine], [], [], NOW);
    expect(result.loops[0]!.isStale).toBe(false);
    expect(result.loops[0]!.state).toBe("ok");
  });

  it("null acceptance_rate renders as 'no judged runs', never 0%", () => {
    const routine = loopRoutine({ acceptance_rate: null });
    const result = deriveLoopHealth([routine], [], [], NOW);
    expect(result.loops[0]!.acceptanceText).toBe("no judged runs");
    expect(result.loops[0]!.acceptanceText).not.toContain("0%");
  });

  it("a non-null acceptance_rate still renders a percentage", () => {
    const routine = loopRoutine({ acceptance_rate: 0.5 });
    const result = deriveLoopHealth([routine], [], [], NOW);
    expect(result.loops[0]!.acceptanceText).toBe("50% accepted");
  });

  // --- ISSUE-8: null total_cost_usd (migration 119) renders "unknown", never $0.00 ---

  it("null total_cost_usd renders as unknown, never $0.00, and never throws", () => {
    const routine = loopRoutine({ total_cost_usd: null });
    const result = deriveLoopHealth([routine], [], [], NOW);
    expect(result.loops[0]!.totalCostUsd).toBeNull();
    expect(result.loops[0]!.totalCostText).toBe("unknown total cost");
    expect(result.loops[0]!.totalCostText).not.toContain("$0.00");
  });

  it("a known, non-null total_cost_usd still renders a dollar figure", () => {
    const routine = loopRoutine({ total_cost_usd: 1.25 });
    const result = deriveLoopHealth([routine], [], [], NOW);
    expect(result.loops[0]!.totalCostText).toBe("$1.25 total");
  });

  it("a genuinely exact zero total_cost_usd still renders $0.00, distinct from unknown", () => {
    const routine = loopRoutine({ total_cost_usd: 0 });
    const result = deriveLoopHealth([routine], [], [], NOW);
    expect(result.loops[0]!.totalCostText).toBe("$0.00 total");
  });

  it("PARKED is amber (warn), never red: a loop with a matched pending approval is needs_you/warn", () => {
    const routine = loopRoutine();
    const result = deriveLoopHealth([routine], [], [approval()], NOW);
    const row = result.loops[0]!;
    expect(row.state).toBe("needs_you");
    expect(row.tone).toBe("warn");
    expect(row.pendingApprovals).toHaveLength(1);
  });

  it("AUTO_PAUSED is red (danger)", () => {
    const routine = loopRoutine({ status: "auto_paused", auto_pause_reason: "acceptance below floor" });
    const result = deriveLoopHealth([routine], [], [], NOW);
    const row = result.loops[0]!;
    expect(row.state).toBe("auto_paused");
    expect(row.tone).toBe("danger");
  });

  it("DISABLED is neutral/grey, dimmed but present -- never dropped from the list", () => {
    const routine = loopRoutine({ status: "disabled", last_fired: null });
    const result = deriveLoopHealth([routine], [], [], NOW);
    const row = result.loops[0]!;
    expect(row.state).toBe("disabled");
    expect(row.tone).toBe("neutral");
  });

  it("an active loop that has never fired is its own state, not silently OK", () => {
    const routine = loopRoutine({ status: "active", last_fired: null });
    const result = deriveLoopHealth([routine], [], [], NOW);
    expect(result.loops[0]!.state).toBe("never_fired");
  });

  it("an unmatched loop-risk approval (renamed/unknown instance) is surfaced, never dropped", () => {
    const routine = loopRoutine(); // instance id "w3_health_monitor"
    const stray = approval({ id: "apr_stray", proposed_action: "w3_prod_path/diagnose: restart" });
    const result = deriveLoopHealth([routine], [], [stray], NOW);
    expect(result.loops[0]!.pendingApprovals).toHaveLength(0);
    expect(result.unmatchedLoopApprovals).toHaveLength(1);
    expect(result.unmatchedLoopApprovals[0]!.id).toBe("apr_stray");
  });

  it("sorts worst state first: needs_you, then stale, then auto_paused, then disabled, then never_fired, then ok", () => {
    const ok = loopRoutine({ id: "r_ok", last_fired: new Date(NOW.getTime() - 4 * 60_000).toISOString() });
    const neverFired = loopRoutine({ id: "r_never", status: "active", last_fired: null });
    const disabled = loopRoutine({ id: "r_disabled", status: "disabled", last_fired: null });
    const autoPaused = loopRoutine({ id: "r_paused", status: "auto_paused" });
    const stale = loopRoutine({ id: "r_stale", last_fired: new Date(NOW.getTime() - 25 * 60_000).toISOString() });
    const needsYou = loopRoutine({
      id: "r_needs_you",
      task_template: {
        input: {
          module: "omniagentos.loops",
          instance_id: "w3_needs_you",
          instance_module: "omniagentos_loops.instances.health_monitor",
        },
      },
    });
    const parkedApproval = approval({ id: "apr_needs_you", proposed_action: "w3_needs_you/diagnose: x" });

    const result = deriveLoopHealth(
      [ok, neverFired, disabled, autoPaused, stale, needsYou],
      [],
      [parkedApproval],
      NOW,
    );
    expect(result.loops.map((row) => row.state)).toEqual([
      "needs_you",
      "stale",
      "auto_paused",
      "disabled",
      "never_fired",
      "ok",
    ]);
  });

  it("builds an oldest-to-newest tick strip from the cross-routine runs aggregate, filtered to this routine", () => {
    const routine = loopRoutine();
    const runs: RecentRunItem[] = [
      run({ run_id: "run_3", finished_at: "2026-08-01T19:50:00Z", gate_passed: null, accepted: null }), // newest
      run({ run_id: "run_other_routine", routine_id: "rtn_other" }),
      run({ run_id: "run_2", finished_at: "2026-08-01T19:40:00Z", gate_passed: true, accepted: true }),
      run({ run_id: "run_1", finished_at: "2026-08-01T19:30:00Z", gate_passed: false, accepted: false }),
    ];
    const result = deriveLoopHealth([routine], runs, [], NOW);
    const row = result.loops[0]!;
    expect(row.ticks.map((t) => t.runId)).toEqual(["run_1", "run_2", "run_3"]);
    expect(row.ticks.map((t) => t.outcome)).toEqual(["adverse", "favourable", "neutral"]);
    expect(row.lastOutcome).toBe("neutral");
  });
});
