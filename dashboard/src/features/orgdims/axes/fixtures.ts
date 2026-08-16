/**
 * Local fixtures for axis primitives, derived from
 * contracts/fixtures/mission/v1/active-context.json shapes.
 *
 * Prefer loading the real fixture file when the path resolves; otherwise
 * embed the three gate entries faithfully.
 */

import type { ActiveContextLike } from "./axisProjection";
import { emptyAxes, projectActiveContext } from "./axisProjection";
import type { AxisKey, AxisRegistry, AxisState, AxisValue } from "./types";

/** Minimal active-context entry shape used by projection tests. */
export type ActiveContextFixture = ActiveContextLike & {
  id: string;
  variant?: string;
};

/**
 * Faithful embeds of ctx_confirmed, ctx_ambiguous, ctx_high_risk from
 * contracts/fixtures/mission/v1/active-context.json.
 */
export const EMBEDDED_ACTIVE_CONTEXT: ActiveContextFixture[] = [
  {
    id: "ctx_confirmed",
    variant: "confirmed",
    execution_ref: {
      company_id: "comp_alpha",
      project_id: "proj_mission_rail",
    },
    status: "confirmed",
    organization_context: {
      company_id: "comp_alpha",
      company_slug: "alpha-robotics",
      product_id: "prod_mission",
      product_slug: "mission-command",
    },
    project_suggestion: {
      project_id: "proj_mission_rail",
      confidence: 0.91,
      rationale: "Explicit operator confirmation of mission rail project",
    },
  },
  {
    id: "ctx_ambiguous",
    variant: "ambiguous",
    execution_ref: {
      company_id: "comp_alpha",
      project_id: null,
    },
    status: "ambiguous",
    organization_context: {
      company_id: "comp_alpha",
      company_slug: "alpha-robotics",
      product_id: null,
      product_slug: null,
    },
    project_suggestion: {
      project_id: null,
      confidence: 0.41,
      rationale: "Two projects score within epsilon; operator must choose",
    },
  },
  {
    id: "ctx_high_risk",
    variant: "high-risk-needs-confirmation",
    execution_ref: {
      company_id: "comp_alpha",
      project_id: "proj_mission_rail",
    },
    status: "needs_confirmation",
    organization_context: {
      company_id: "comp_alpha",
      company_slug: "alpha-robotics",
      product_id: "prod_mission",
      product_slug: "mission-command",
    },
    project_suggestion: {
      project_id: "proj_mission_rail",
      confidence: 0.8,
      rationale: "High-risk classification requires explicit confirmation",
    },
  },
];

function loadActiveContextFixtures(): ActiveContextFixture[] {
  try {
    // Relative from dashboard/src/features/orgdims/axes → repo contracts.
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const mod = require("../../../../../contracts/fixtures/mission/v1/active-context.json") as
      | ActiveContextFixture[]
      | { default: ActiveContextFixture[] };
    const list = Array.isArray(mod) ? mod : mod.default;
    if (Array.isArray(list) && list.length > 0) {
      return list as ActiveContextFixture[];
    }
  } catch {
    // Vitest/bundle may not resolve outside dashboard — use embeds.
  }
  return EMBEDDED_ACTIVE_CONTEXT;
}

export const ACTIVE_CONTEXT_FIXTURES: ActiveContextFixture[] =
  loadActiveContextFixtures();

export function getActiveContextById(
  id: string,
): ActiveContextFixture | undefined {
  return (
    ACTIVE_CONTEXT_FIXTURES.find((f) => f.id === id) ??
    EMBEDDED_ACTIVE_CONTEXT.find((f) => f.id === id)
  );
}

/** Bare four-missing states (four_axes_explicit_unknown). */
export function fixtureEmptyAxes(): Record<AxisKey, AxisState> {
  return emptyAxes();
}

function value(
  id: string,
  slug: string,
  label: string,
): AxisValue {
  return { id, slug, label };
}

function applied(
  axis: AxisKey,
  v: AxisValue,
  extras: Partial<AxisState> = {},
): AxisState {
  return {
    axis,
    value: v,
    resolution: "applied",
    confidence: null,
    source: "fixture",
    rationale: null,
    locked: false,
    editable: true,
    pending: false,
    ...extras,
  };
}

/** Full applied four-axis sample for layout tests. */
export function fixtureFullAppliedAxes(): Record<AxisKey, AxisState> {
  return {
    company: applied("company", value("comp_alpha", "alpha-robotics", "Alpha Robotics")),
    project: applied(
      "project",
      value("proj_mission_rail", "mission-rail", "Mission Rail"),
      { confidence: 0.91, source: "project_suggestion" },
    ),
    workstream: applied(
      "workstream",
      value("ws_delivery", "delivery", "Delivery"),
    ),
    work_kind: applied(
      "work_kind",
      value("wk_implementation", "implementation", "Implementation"),
    ),
  };
}

/**
 * Unknown work_kind sample with injected registry option `legal_review`.
 * registry_not_hardcoded: option is injected, not a component allowlist.
 */
export function fixtureUnknownWorkKind(): {
  axes: Record<AxisKey, AxisState>;
  registry: AxisRegistry;
} {
  const registry: AxisRegistry = {
    work_kind: [
      {
        id: "wk_legal_review",
        slug: "legal_review",
        label: "legal_review",
      },
    ],
  };

  const axes = fixtureFullAppliedAxes();
  axes.work_kind = {
    axis: "work_kind",
    value: {
      id: "wk_legal_review",
      slug: "legal_review",
      label: "legal_review",
    },
    resolution: "applied",
    confidence: null,
    source: "registry",
    rationale: null,
    locked: false,
    editable: true,
    pending: false,
  };

  return { axes, registry };
}

/** Project confirmed / ambiguous / high-risk via projection helper. */
export function projectFixtureById(id: string) {
  const entry = getActiveContextById(id);
  if (!entry) {
    return projectActiveContext(null);
  }
  return projectActiveContext(entry);
}

export const FIXTURE_IDS = {
  confirmed: "ctx_confirmed",
  ambiguous: "ctx_ambiguous",
  highRisk: "ctx_high_risk",
} as const;
