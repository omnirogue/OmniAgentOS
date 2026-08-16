/**
 * Shared classification-axis primitives (P1-FE-AXES).
 *
 * Four independent axes; no client-side bind policy lives here.
 */

export type AxisKey = "company" | "project" | "workstream" | "work_kind";

export type AxisResolution =
  | "missing"
  | "suggested"
  | "provisional"
  | "applied"
  | "locked"
  | "rejected";

export type AxisValue = { id: string; slug: string; label: string };

export type AxisState = {
  axis: AxisKey;
  value: AxisValue | null;
  resolution: AxisResolution;
  confidence: number | null;
  source: string | null;
  rationale: string | null;
  locked: boolean;
  editable: boolean;
  pending: boolean;
};

/** Stable left-to-right order for every chip row. */
export const AXIS_ORDER: AxisKey[] = [
  "company",
  "project",
  "workstream",
  "work_kind",
];

/** Human-readable axis titles. */
export const AXIS_LABELS: Record<AxisKey, string> = {
  company: "Company",
  project: "Project",
  workstream: "Workstream",
  work_kind: "Work kind",
};

/** Injected registry option — never hardcoded slug allowlists in components. */
export type AxisRegistryOption = { id: string; slug: string; label: string };

/** Partial registry keyed by axis; hosts inject options. */
export type AxisRegistry = Partial<Record<AxisKey, AxisRegistryOption[]>>;
