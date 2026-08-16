/**
 * Map P0 active-context style payloads into four-axis AxisState records.
 *
 * Confidence is display data only — never a bind threshold.
 * Workstream / work_kind stay missing until P2 supplies them.
 */

import {
  AXIS_ORDER,
  type AxisKey,
  type AxisRegistry,
  type AxisRegistryOption,
  type AxisResolution,
  type AxisState,
  type AxisValue,
} from "./types";

/** Loose P0 active-context shape (subset we read). */
export type ActiveContextLike = {
  id?: string;
  status?: string | null;
  locked?: boolean | null;
  execution_ref?: {
    company_id?: string | null;
    project_id?: string | null;
  } | null;
  organization_context?: {
    company_id?: string | null;
    company_slug?: string | null;
    product_id?: string | null;
    product_slug?: string | null;
  } | null;
  project_suggestion?: {
    project_id?: string | null;
    confidence?: number | null;
    rationale?: string | null;
    name?: string | null;
  } | null;
  previous_project_id?: string | null;
  /** Explicit lock flag from host/server if present. */
  axis_locks?: Partial<Record<AxisKey, boolean>> | null;
};

export type AxisProjectionOptions = {
  /** Injected options for id/slug → label resolution. */
  registry?: AxisRegistry;
};

export type AxisProjectionResult = {
  axes: Record<AxisKey, AxisState>;
  /** Non-null when input was unusable; axes are four explicit missing states. */
  error: string | null;
};

function emptyMissing(axis: AxisKey): AxisState {
  return {
    axis,
    value: null,
    resolution: "missing",
    confidence: null,
    source: null,
    rationale: null,
    locked: false,
    editable: true,
    pending: false,
  };
}

/** Four explicit missing axes — used for bare records and projection errors. */
export function emptyAxes(): Record<AxisKey, AxisState> {
  return {
    company: emptyMissing("company"),
    project: emptyMissing("project"),
    workstream: emptyMissing("workstream"),
    work_kind: emptyMissing("work_kind"),
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Server status → project resolution. Confidence is never consulted.
 *
 * client_threshold_counterfeit gate: a mutation that maps
 * `confidence >= 0.8 → applied` must fail axisProjection tests.
 * High confidence + status needs_confirmation remains **suggested**.
 */
function projectResolutionFromStatus(
  status: string | null | undefined,
  projectId: string | null,
  explicitlyLocked: boolean,
): AxisResolution {
  if (explicitlyLocked) return "locked";
  const s = (status ?? "").toLowerCase();

  if (s === "locked") return "locked";
  if (s === "rejected") return projectId ? "rejected" : "missing";

  if (
    s === "confirmed" ||
    s === "applied" ||
    s === "corrected" ||
    s === "accepted"
  ) {
    return projectId ? "applied" : "missing";
  }

  // Needs confirmation / suggested / ambiguous / reclass — never auto-applied
  // from confidence or any other client number.
  if (
    s === "suggested" ||
    s === "needs_confirmation" ||
    s === "ambiguous" ||
    s === "reclassification_pending" ||
    s === "provisional" ||
    s === "stale"
  ) {
    if (!projectId) return "missing";
    if (s === "reclassification_pending" || s === "provisional") {
      return "provisional";
    }
    return "suggested";
  }

  // Unknown status: value present → suggested (safe); absent → missing.
  return projectId ? "suggested" : "missing";
}

function companyResolutionFromStatus(
  status: string | null | undefined,
  companyId: string | null,
  explicitlyLocked: boolean,
): AxisResolution {
  if (explicitlyLocked) return "locked";
  if (!companyId) return "missing";
  const s = (status ?? "").toLowerCase();
  if (s === "locked") return "locked";
  if (s === "rejected") return "rejected";
  if (s === "reclassification_pending" || s === "provisional") {
    return "provisional";
  }
  // Company from org context is a present fact when id is supplied.
  return "applied";
}

function humanizeSlug(slug: string): string {
  return slug
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function findRegistryOption(
  options: AxisRegistryOption[] | undefined,
  id: string | null | undefined,
  slug: string | null | undefined,
): AxisRegistryOption | undefined {
  if (!options?.length) return undefined;
  if (id) {
    const byId = options.find((o) => o.id === id);
    if (byId) return byId;
  }
  if (slug) {
    const bySlug = options.find((o) => o.slug === slug);
    if (bySlug) return bySlug;
  }
  return undefined;
}

function resolveValue(
  axis: AxisKey,
  id: string | null | undefined,
  slug: string | null | undefined,
  label: string | null | undefined,
  registry?: AxisRegistry,
): AxisValue | null {
  if (!id && !slug && !label) return null;
  const reg = findRegistryOption(registry?.[axis], id, slug);
  if (reg) {
    return { id: reg.id, slug: reg.slug, label: reg.label };
  }
  const resolvedId = id ?? slug ?? label ?? "";
  const resolvedSlug = slug ?? id ?? "";
  const resolvedLabel =
    label ?? (slug ? humanizeSlug(slug) : id ? id : "Unknown");
  return {
    id: resolvedId,
    slug: resolvedSlug,
    label: resolvedLabel,
  };
}

function finalizeState(
  partial: Omit<AxisState, "locked" | "editable" | "pending"> & {
    locked?: boolean;
  },
): AxisState {
  const locked = partial.locked === true || partial.resolution === "locked";
  const resolution: AxisResolution = locked ? "locked" : partial.resolution;
  return {
    ...partial,
    resolution,
    locked,
    editable: !locked && resolution !== "rejected",
    pending: resolution === "provisional" || resolution === "suggested",
  };
}

/**
 * Resolve project id from suggestion (including explicit null) then execution_ref.
 * Never invents a project when suggestion.project_id is null.
 */
function resolveProjectId(ctx: ActiveContextLike): string | null {
  const suggestion = ctx.project_suggestion;
  if (suggestion && Object.prototype.hasOwnProperty.call(suggestion, "project_id")) {
    return typeof suggestion.project_id === "string" ? suggestion.project_id : null;
  }
  const execId = ctx.execution_ref?.project_id;
  return typeof execId === "string" ? execId : null;
}

/**
 * Project an active-context-like payload into four axis states.
 * On error: four missing axes + error string (never favorable fixture data).
 */
export function projectActiveContext(
  payload: unknown,
  options: AxisProjectionOptions = {},
): AxisProjectionResult {
  if (payload == null) {
    return {
      axes: emptyAxes(),
      error: "active context payload is null or undefined",
    };
  }
  if (!isRecord(payload)) {
    return {
      axes: emptyAxes(),
      error: "active context payload must be an object",
    };
  }

  try {
    const ctx = payload as ActiveContextLike;
    const registry = options.registry;
    const status = ctx.status ?? null;

    const org = ctx.organization_context ?? null;
    const exec = ctx.execution_ref ?? null;
    const suggestion = ctx.project_suggestion ?? null;

    const companyId =
      (typeof org?.company_id === "string" && org.company_id) ||
      (typeof exec?.company_id === "string" && exec.company_id) ||
      null;
    const companySlug =
      typeof org?.company_slug === "string" ? org.company_slug : null;

    const resolvedProjectId = resolveProjectId(ctx);

    const confidence =
      suggestion && typeof suggestion.confidence === "number"
        ? suggestion.confidence
        : null;
    const rationale =
      suggestion && typeof suggestion.rationale === "string"
        ? suggestion.rationale
        : null;

    const companyLocked = Boolean(ctx.axis_locks?.company || ctx.locked);
    const projectLocked = Boolean(ctx.axis_locks?.project || ctx.locked);

    const companyValue = resolveValue(
      "company",
      companyId,
      companySlug,
      companySlug ? humanizeSlug(companySlug) : null,
      registry,
    );

    const projectName =
      suggestion && typeof suggestion.name === "string" ? suggestion.name : null;
    const projectValue = resolveValue(
      "project",
      resolvedProjectId,
      resolvedProjectId,
      projectName,
      registry,
    );

    let projectRationale = rationale;
    if (
      (status ?? "").toLowerCase() === "corrected" &&
      typeof ctx.previous_project_id === "string"
    ) {
      const prior = ctx.previous_project_id;
      projectRationale = rationale
        ? `${rationale} (previous: ${prior})`
        : `Corrected from ${prior}`;
    }

    const companyRes = companyResolutionFromStatus(
      status,
      companyId,
      companyLocked,
    );
    const projectRes = projectResolutionFromStatus(
      status,
      resolvedProjectId,
      projectLocked,
    );

    const axes: Record<AxisKey, AxisState> = {
      company: finalizeState({
        axis: "company",
        value: companyValue,
        resolution: companyRes,
        confidence: null,
        source: companyId ? "organization_context" : null,
        rationale: null,
        locked: companyLocked,
      }),
      project: finalizeState({
        axis: "project",
        value: projectRes === "missing" ? null : projectValue,
        resolution: projectRes,
        // Confidence is display-only; never used to choose resolution above.
        confidence,
        source: suggestion
          ? "project_suggestion"
          : exec
            ? "execution_ref"
            : null,
        rationale: projectRationale,
        locked: projectLocked,
      }),
      // P0 active-context does not supply these axes.
      workstream: emptyMissing("workstream"),
      work_kind: emptyMissing("work_kind"),
    };

    // Ensure every key in AXIS_ORDER exists (four_axes_explicit_unknown).
    for (const key of AXIS_ORDER) {
      if (!axes[key]) axes[key] = emptyMissing(key);
    }

    return { axes, error: null };
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "unknown projection error";
    return {
      axes: emptyAxes(),
      error: `projection failed: ${message}`,
    };
  }
}
