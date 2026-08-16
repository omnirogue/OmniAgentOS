"use client";

import {
  AXIS_ORDER,
  type AxisKey,
  type AxisRegistry,
  type AxisState,
} from "./types";
import { emptyAxes } from "./axisProjection";
import { AxisChip } from "./AxisChip";
import styles from "./axes.module.css";

export type AxisChipRowProps = {
  /** Explicit four-axis state map. Missing keys render as Unassigned. */
  axes?: Partial<Record<AxisKey, AxisState>> | null;
  /** Optional activate handler per axis (no auto-bind). */
  onActivate?: (axis: AxisKey) => void;
  /**
   * Injected registry only — never used as a hardcoded allowlist.
   * Unknown slugs (e.g. legal_review) render their supplied label/state.
   */
  registry?: AxisRegistry;
  className?: string;
  "aria-label"?: string;
};

function resolveAxes(
  input: Partial<Record<AxisKey, AxisState>> | null | undefined,
): Record<AxisKey, AxisState> {
  const base = emptyAxes();
  if (!input) return base;
  for (const key of AXIS_ORDER) {
    const state = input[key];
    if (state) {
      base[key] = { ...state, axis: key };
    }
  }
  return base;
}

/**
 * Exactly four chips in stable order: Company → Project → Workstream → Work kind.
 * Never omits missing axes; never auto-binds authority from confidence.
 */
export function AxisChipRow({
  axes,
  onActivate,
  registry: _registry,
  className,
  "aria-label": ariaLabel = "Classification axes",
}: AxisChipRowProps) {
  // registry is accepted for host injection symmetry; labels come from AxisState.
  // Components must not filter against hardcoded slug lists (registry_not_hardcoded).
  void _registry;

  const resolved = resolveAxes(axes);

  return (
    <div
      className={[styles.row, className].filter(Boolean).join(" ")}
      role="group"
      aria-label={ariaLabel}
      data-axis-row="true"
      data-axis-count={AXIS_ORDER.length}
    >
      {AXIS_ORDER.map((key) => {
        const state = resolved[key];
        return (
          <AxisChip
            key={key}
            state={state}
            onActivate={onActivate}
          />
        );
      })}
    </div>
  );
}
