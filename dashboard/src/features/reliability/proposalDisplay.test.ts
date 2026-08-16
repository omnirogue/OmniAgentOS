/**
 * TDD contract for governed improvement proposal display + self-approval.
 * Implements design: docs/ui-redesign/improvements-governed-evidence-design.md §2.2–2.3 / §4.1–4.2
 *
 * Product module under test (implementer): ./proposalDisplay.ts
 */
import { describe, expect, it } from "vitest";
import type { ApiImprovement } from "../../lib/reliabilityContracts";
import {
  assertCanDecide,
  isSelfApproval,
  toGovernedProposalView,
} from "./proposalDisplay";

const FIXED_TS = "2026-08-01T12:00:00.000Z";

function baseImprovement(overrides: Partial<ApiImprovement> = {}): ApiImprovement {
  return {
    id: "imp-gov-1",
    origin: "csi",
    kind: "code_fix",
    title: "Harden improvement approve path",
    summary: "Surface evidence and block self-approval on the improvements card.",
    root_cause: "Operators approved without seeing plan or author identity.",
    proposal_json: {
      goal: "Show governed proposal fields before approve",
      branch: "project/grok-improvements-evidence-0812",
      git_branch: "should-not-override-branch",
      change_type: "dashboard-ui",
      plan: ["Add proposalDisplay mapper", "Wire identity dialog", "Add self-approval guard"],
      repro: "Open /improvements with an awaiting_human item",
      expected_impact: "Faster, safer human review of improvement proposals",
      risk_hint: "Low UI-only change; no backend auth change",
      files: [{ path: "dashboard/src/app/improvements/page.tsx" }, "dashboard/src/features/reliability/proposalDisplay.ts"],
      config_edits: [{ path: "dashboard/src/features/reliability/improvements.module.css" }],
    },
    risk_level: 2,
    status: "awaiting_human",
    sandbox_json: {
      passed: true,
      report: "sandbox: unit suite green; typecheck green",
      risk_reasons: ["touches governance UI", "identity dialog required"],
      risk_tier: "L2",
    },
    votes_summary_json: { steward: "approve", critic: "needs_human" },
    rollback_point_id: "rbp-abc123",
    applied_sha: null,
    monitor_until: null,
    decided_by: null,
    created_by: "agent-planner",
    created_at: FIXED_TS,
    updated_at: FIXED_TS,
    applied_at: null,
    resolved_at: null,
    before: "Card showed title/summary only",
    after: "Card shows eight governed sections",
    ...overrides,
  };
}

describe("toGovernedProposalView — full fixture", () => {
  it("projects evidence, benefit, scope, risks, approval, execution, verification, rollback", () => {
    const view = toGovernedProposalView(baseImprovement());

    expect(view.evidence.items.join("\n")).toMatch(/Operators approved without seeing plan/);
    expect(view.evidence.items.some((line) => /proposalDisplay mapper/.test(line))).toBe(true);
    expect(view.evidence.items.some((line) => /Open \/improvements/.test(line))).toBe(true);
    expect(view.evidence.emptyLabel).toBe("Not provided");

    expect(view.expectedBenefit).toBe(
      "Faster, safer human review of improvement proposals",
    );

    expect(view.scope.changeType).toBe("dashboard-ui");
    expect(view.scope.kind).toBe("code_fix");
    expect(view.scope.origin).toBe("csi");
    expect(view.scope.paths).toEqual(
      expect.arrayContaining([
        "dashboard/src/app/improvements/page.tsx",
        "dashboard/src/features/reliability/proposalDisplay.ts",
        "dashboard/src/features/reliability/improvements.module.css",
      ]),
    );

    expect(view.risks.level).toBe(2);
    expect(view.risks.reasons).toEqual(
      expect.arrayContaining(["touches governance UI", "identity dialog required"]),
    );
    expect(view.risks.narrative).toMatch(/Low UI-only change/);

    expect(view.approval.createdBy).toBe("agent-planner");
    expect(view.approval.decidedBy).toBeNull();
    expect(view.approval.status).toBe("awaiting_human");

    expect(view.execution.goal).toBe("Show governed proposal fields before approve");
    expect(view.execution.branch).toBe("project/grok-improvements-evidence-0812");
    expect(view.execution.plan).toEqual(
      expect.arrayContaining(["Wire identity dialog", "Add self-approval guard"]),
    );

    expect(view.verification.sandboxPassed).toBe(true);
    expect(view.verification.report).toMatch(/unit suite green/);
    expect(view.verification.riskTier).toMatch(/L2|2/);
    expect(view.verification.votes).toEqual(
      expect.arrayContaining([
        ["steward", "approve"],
        ["critic", "needs_human"],
      ]),
    );

    expect(view.rollback.pointId).toBe("rbp-abc123");
    expect(view.rollback.appliedSha).toBeNull();
  });
});

describe("toGovernedProposalView — empty / sparse fallbacks", () => {
  it("always returns every section key with Not provided / empty lists / dash-ready nulls", () => {
    const view = toGovernedProposalView(
      baseImprovement({
        summary: "",
        root_cause: "",
        proposal_json: {},
        sandbox_json: {},
        votes_summary_json: {},
        rollback_point_id: null,
        applied_sha: null,
        decided_by: null,
        before: null,
        after: null,
      }),
    );

    expect(view).toEqual(
      expect.objectContaining({
        evidence: expect.objectContaining({
          items: [],
          emptyLabel: "Not provided",
        }),
        expectedBenefit: "Not provided",
        scope: expect.objectContaining({
          changeType: expect.any(String),
          paths: [],
          kind: "code_fix",
          origin: "csi",
        }),
        risks: expect.objectContaining({
          level: 2,
          reasons: [],
          narrative: expect.any(String),
        }),
        approval: expect.objectContaining({
          createdBy: "agent-planner",
          decidedBy: null,
          status: "awaiting_human",
        }),
        execution: expect.objectContaining({
          goal: expect.any(String),
          branch: "Not provided",
          plan: [],
        }),
        verification: expect.objectContaining({
          sandboxPassed: null,
          report: expect.any(String),
          riskTier: expect.any(String),
          votes: [],
        }),
        rollback: { pointId: null, appliedSha: null },
      }),
    );

    // Goal falls back to title when proposal_json.goal absent
    expect(view.execution.goal).toBe("Harden improvement approve path");
    // Benefit never blank string
    expect(view.expectedBenefit.trim().length).toBeGreaterThan(0);
  });

  it("prefers expected_benefit / predicted_impact when expected_impact missing", () => {
    expect(
      toGovernedProposalView(
        baseImprovement({
          proposal_json: { expected_benefit: "Benefit via expected_benefit" },
        }),
      ).expectedBenefit,
    ).toBe("Benefit via expected_benefit");

    expect(
      toGovernedProposalView(
        baseImprovement({
          proposal_json: { predicted_impact: "Benefit via predicted_impact" },
        }),
      ).expectedBenefit,
    ).toBe("Benefit via predicted_impact");
  });

  it("uses git_branch when branch is absent", () => {
    const view = toGovernedProposalView(
      baseImprovement({
        proposal_json: { git_branch: "feature/from-git-branch" },
      }),
    );
    expect(view.execution.branch).toBe("feature/from-git-branch");
  });

  it("never defaults missing/unparseable risk_level to 0 (favourable-absence guard)", () => {
    const missing = toGovernedProposalView(
      baseImprovement({ risk_level: undefined as unknown as number, sandbox_json: {} }),
    );
    expect(missing.risks.level).toBeNull();
    expect(missing.risks.level).not.toBe(0);
    expect(missing.verification.riskTier).toBe("unknown");

    const unparseable = toGovernedProposalView(
      baseImprovement({ risk_level: "not-a-number" as unknown as number, sandbox_json: {} }),
    );
    expect(unparseable.risks.level).toBeNull();
    expect(unparseable.risks.level).not.toBe(0);

    const nullRisk = toGovernedProposalView(
      baseImprovement({ risk_level: null as unknown as number }),
    );
    expect(nullRisk.risks.level).toBeNull();
  });
});

describe("isSelfApproval / assertCanDecide", () => {
  it("treats case-insensitive trimmed identity match as self-approval", () => {
    expect(isSelfApproval("the operator", "owner")).toBe(true);
    expect(isSelfApproval("  AGENT-X  ", "agent-x")).toBe(true);
    expect(isSelfApproval("operator", "csi")).toBe(false);
    expect(isSelfApproval("human", "agent-planner")).toBe(false);
  });

  it("blocks approve when decided_by equals created_by", () => {
    expect(() => assertCanDecide("approve", "agent-x", "agent-x")).toThrow(/self-approval/i);
  });

  it("allows approve when decided_by differs from created_by", () => {
    expect(() => assertCanDecide("approve", "human", "agent-x")).not.toThrow();
  });

  it("blocks approve when decided_by is empty or whitespace", () => {
    expect(() => assertCanDecide("approve", "  ", "agent-x")).toThrow();
    expect(() => assertCanDecide("approve", "", "agent-x")).toThrow();
  });

  it("allows reject of own proposal (self-reject is not self-approval)", () => {
    expect(() => assertCanDecide("reject", "agent-x", "agent-x")).not.toThrow();
  });

  it("requires non-empty decided_by for reject/rollback/pull", () => {
    expect(() => assertCanDecide("reject", "", "agent-x")).toThrow();
    expect(() => assertCanDecide("rollback", "   ", "agent-x")).toThrow();
    expect(() => assertCanDecide("pull", "", "agent-x")).toThrow();
    expect(() => assertCanDecide("pull", "reviewer", "agent-x")).not.toThrow();
  });

  it("fails closed on malformed identity input rather than throwing a raw TypeError", () => {
    expect(() => isSelfApproval("Operator", undefined as unknown as string)).not.toThrow();
    expect(isSelfApproval("Operator", undefined as unknown as string)).toBe(false);
    expect(() => isSelfApproval(undefined as unknown as string, "created-by")).not.toThrow();
    expect(isSelfApproval(undefined as unknown as string, "created-by")).toBe(false);
  });

  it("NFC-normalizes before case-folding, so combining-mark and precomposed forms match", () => {
    // "Ågent" = "A" + COMBINING RING ABOVE + "gent" (decomposed form);
    // "Ågent" = "Å" (precomposed LATIN CAPITAL LETTER A WITH RING ABOVE)
    // + "gent". These are the same string under Unicode NFC normalization,
    // and must match server-side (_normalize_identity in
    // omniagentos/api/routes/improvements.py) as well as here.
    expect(isSelfApproval("Ågent", "Ågent")).toBe(true);
    expect(isSelfApproval("ågent", "ågent")).toBe(true);
    // Different identities, even after normalization, still do not match.
    expect(isSelfApproval("Ågent", "agent")).toBe(false);
  });
});
