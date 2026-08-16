import { describe, expect, it } from "vitest";
import { emptyAxes, projectActiveContext } from "./axisProjection";
import {
  EMBEDDED_ACTIVE_CONTEXT,
  FIXTURE_IDS,
  getActiveContextById,
  projectFixtureById,
} from "./fixtures";
import { AXIS_ORDER, type AxisKey } from "./types";

function assertFourAxes(axes: Record<AxisKey, unknown>) {
  expect(Object.keys(axes).sort()).toEqual([...AXIS_ORDER].sort());
  for (const key of AXIS_ORDER) {
    expect(axes[key]).toBeDefined();
  }
}

describe("projectActiveContext", () => {
  it("maps ctx_confirmed with applied company/project; workstream/work_kind missing", () => {
    const fixture = getActiveContextById(FIXTURE_IDS.confirmed);
    expect(fixture).toBeDefined();
    const { axes, error } = projectActiveContext(fixture);
    expect(error).toBeNull();
    assertFourAxes(axes);

    expect(axes.company.resolution).toBe("applied");
    expect(axes.company.value?.id).toBe("comp_alpha");
    expect(axes.company.value?.slug).toBe("alpha-robotics");

    expect(axes.project.resolution).toBe("applied");
    expect(axes.project.value?.id).toBe("proj_mission_rail");
    expect(axes.project.confidence).toBe(0.91);

    // P0 does not supply these — stay explicit missing
    expect(axes.workstream.resolution).toBe("missing");
    expect(axes.workstream.value).toBeNull();
    expect(axes.work_kind.resolution).toBe("missing");
    expect(axes.work_kind.value).toBeNull();
  });

  it("maps ctx_ambiguous: project null → missing; never invents a project", () => {
    const fixture = getActiveContextById(FIXTURE_IDS.ambiguous);
    expect(fixture).toBeDefined();
    const { axes, error } = projectActiveContext(fixture);
    expect(error).toBeNull();
    assertFourAxes(axes);

    expect(axes.company.value?.id).toBe("comp_alpha");
    // Project id is null — must be missing (or suggested-without-value);
    // never invent proj_mission_rail from candidates or company.
    expect(axes.project.value).toBeNull();
    expect(axes.project.resolution).toBe("missing");
    expect(axes.project.confidence).toBe(0.41);

    expect(axes.workstream.resolution).toBe("missing");
    expect(axes.work_kind.resolution).toBe("missing");
  });

  it("client_threshold_counterfeit: high-risk needs_confirmation stays suggested even when confidence >= 0.8", () => {
    // Gate: high-risk fixture (confidence 0.8, status needs_confirmation)
    // MUST remain suggested/not applied. Confidence is display data only.
    //
    // A counterfeit mutation that changes projection to
    // `if (confidence >= 0.8) resolution = "applied"` MUST fail this test.
    const fixture = getActiveContextById(FIXTURE_IDS.highRisk);
    expect(fixture).toBeDefined();
    expect(fixture?.status).toBe("needs_confirmation");
    expect(fixture?.project_suggestion?.confidence).toBe(0.8);

    const { axes, error } = projectActiveContext(fixture);
    expect(error).toBeNull();

    expect(axes.project.confidence).toBe(0.8);
    expect(axes.project.confidence! >= 0.8).toBe(true);
    // MUST NOT become applied solely because confidence crossed a client threshold
    expect(axes.project.resolution).not.toBe("applied");
    expect(axes.project.resolution).toBe("suggested");
    expect(axes.project.value?.id).toBe("proj_mission_rail");

    // Even with artificially higher confidence, status still wins
    const boosted = {
      ...fixture!,
      project_suggestion: {
        ...fixture!.project_suggestion!,
        confidence: 0.99,
      },
    };
    const boostedResult = projectActiveContext(boosted);
    expect(boostedResult.axes.project.confidence).toBe(0.99);
    expect(boostedResult.axes.project.resolution).toBe("suggested");
    expect(boostedResult.axes.project.resolution).not.toBe("applied");

    // workstream / work_kind stay missing (no inference from confidence)
    expect(axes.workstream.resolution).toBe("missing");
    expect(axes.work_kind.resolution).toBe("missing");
  });

  it("does not infer workstream/work_kind from work.kind, depth, or slug lists", () => {
    const polluted = {
      ...getActiveContextById(FIXTURE_IDS.confirmed)!,
      work: { kind: "legal_review", depth: 3 },
      classification: { primary_workstream: "delivery" },
    };
    const { axes } = projectActiveContext(polluted);
    expect(axes.workstream.resolution).toBe("missing");
    expect(axes.workstream.value).toBeNull();
    expect(axes.work_kind.resolution).toBe("missing");
    expect(axes.work_kind.value).toBeNull();
  });

  it("on projection error returns four missing states + error string", () => {
    const { axes, error } = projectActiveContext(null);
    expect(error).toBeTruthy();
    assertFourAxes(axes);
    for (const key of AXIS_ORDER) {
      expect(axes[key].resolution).toBe("missing");
      expect(axes[key].value).toBeNull();
    }
    // Never substitute favorable fixture data
    expect(axes.project.resolution).not.toBe("applied");
  });

  it("on non-object payload returns four missing + error", () => {
    const { axes, error } = projectActiveContext("not-an-object");
    expect(error).toMatch(/object/i);
    expect(axes).toEqual(emptyAxes());
  });

  it("uses injected registry for label resolution without hardcoding slugs", () => {
    const fixture = getActiveContextById(FIXTURE_IDS.confirmed)!;
    const { axes } = projectActiveContext(fixture, {
      registry: {
        project: [
          {
            id: "proj_mission_rail",
            slug: "mission-rail",
            label: "Mission Rail (registry)",
          },
        ],
      },
    });
    expect(axes.project.value?.label).toBe("Mission Rail (registry)");
  });

  it("locked only when explicitly locked by input, never by confidence", () => {
    const fixture = {
      ...getActiveContextById(FIXTURE_IDS.highRisk)!,
      project_suggestion: {
        project_id: "proj_mission_rail",
        confidence: 0.99,
        rationale: "high conf",
      },
    };
    const unlocked = projectActiveContext(fixture);
    expect(unlocked.axes.project.locked).toBe(false);
    expect(unlocked.axes.project.resolution).toBe("suggested");

    const locked = projectActiveContext({
      ...fixture,
      axis_locks: { project: true },
    });
    expect(locked.axes.project.locked).toBe(true);
    expect(locked.axes.project.resolution).toBe("locked");
  });

  it("projectFixtureById helper covers the three gate fixtures", () => {
    for (const id of [
      FIXTURE_IDS.confirmed,
      FIXTURE_IDS.ambiguous,
      FIXTURE_IDS.highRisk,
    ]) {
      const result = projectFixtureById(id);
      expect(result.error).toBeNull();
      assertFourAxes(result.axes);
    }
    // Embeds always include the three gate ids
    const ids = EMBEDDED_ACTIVE_CONTEXT.map((f) => f.id);
    expect(ids).toContain("ctx_confirmed");
    expect(ids).toContain("ctx_ambiguous");
    expect(ids).toContain("ctx_high_risk");
  });
});
