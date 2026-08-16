"use client";

import type { KeyboardEvent, MouseEvent, ReactNode } from "react";
import { AXIS_LABELS, type AxisKey, type AxisResolution, type AxisState } from "./types";
import styles from "./axes.module.css";

/** Non-color glyph per resolution — state is not color-only. */
export const RESOLUTION_GLYPH: Record<AxisResolution, string> = {
  missing: "○",
  suggested: "?",
  provisional: "~",
  applied: "●",
  locked: "■",
  rejected: "×",
};

export const RESOLUTION_LABEL: Record<AxisResolution, string> = {
  missing: "missing",
  suggested: "suggested",
  provisional: "provisional",
  applied: "applied",
  locked: "locked",
  rejected: "rejected",
};

export type AxisChipProps = {
  state: AxisState;
  /** Invoked on click / Enter / Space when editable and not locked. */
  onActivate?: (axis: AxisKey) => void;
  className?: string;
  children?: ReactNode;
};

function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

/** Visible value text: label or "Unassigned <axis>". */
export function axisValueText(state: AxisState): string {
  if (state.value?.label) return state.value.label;
  const axisWord = state.axis === "work_kind" ? "work kind" : state.axis.replace("_", " ");
  return `Unassigned ${axisWord}`;
}

/** Accessible name: axis, value/Unassigned, resolution, confidence when present. */
export function axisAccessibleName(state: AxisState): string {
  const axisLabel = AXIS_LABELS[state.axis];
  const valuePart = state.value?.label
    ? state.value.label
    : `Unassigned ${state.axis === "work_kind" ? "work kind" : state.axis.replace("_", " ")}`;
  const res = RESOLUTION_LABEL[state.resolution];
  const parts = [axisLabel, valuePart, res];
  if (state.confidence != null && Number.isFinite(state.confidence)) {
    parts.push(`confidence ${state.confidence}`);
  }
  return parts.join(", ");
}

function canActivate(state: AxisState): boolean {
  return state.editable && !state.locked && state.resolution !== "locked";
}

export function AxisChip({ state, onActivate, className, children }: AxisChipProps) {
  const interactive = Boolean(onActivate) && canActivate(state);
  const glyph = RESOLUTION_GLYPH[state.resolution];
  const valueText = axisValueText(state);
  const unassigned = !state.value;
  const name = axisAccessibleName(state);
  const axisLabel = AXIS_LABELS[state.axis];

  const activate = () => {
    if (!interactive || !onActivate) return;
    onActivate(state.axis);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (!interactive) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      activate();
    }
  };

  /**
   * Nested-link activation (e.g. host wraps chips in a row link):
   * stop the event so parent navigation does not fire.
   */
  const onClick = (event: MouseEvent<HTMLButtonElement>) => {
    if (interactive) {
      event.preventDefault();
      event.stopPropagation();
      activate();
      return;
    }
    // Non-interactive chips still swallow nested activation so a parent link
    // is not triggered from the chip surface when locked/non-editable.
    if (event.defaultPrevented) return;
  };

  const onNestedActivate = (event: MouseEvent | KeyboardEvent) => {
    event.preventDefault();
    event.stopPropagation();
  };

  return (
    <button
      type="button"
      className={cx(
        styles.chip,
        interactive && styles.chipInteractive,
        (state.locked || state.resolution === "locked") && styles.chipLocked,
        unassigned && styles.chipMissing,
        className,
      )}
      data-axis={state.axis}
      data-resolution={state.resolution}
      data-locked={state.locked ? "true" : "false"}
      data-editable={state.editable ? "true" : "false"}
      aria-label={name}
      aria-disabled={!interactive || undefined}
      disabled={false}
      tabIndex={interactive ? 0 : -1}
      onClick={onClick}
      onKeyDown={onKeyDown}
      onMouseDown={interactive ? onNestedActivate : undefined}
    >
      <span
        className={styles.glyph}
        data-glyph={state.resolution}
        aria-hidden="true"
        title={RESOLUTION_LABEL[state.resolution]}
      >
        {glyph}
      </span>
      <span className={styles.axisName}>{axisLabel}</span>
      <span
        className={cx(styles.value, unassigned && styles.valueUnassigned)}
        data-unassigned={unassigned ? "true" : "false"}
      >
        {valueText}
      </span>
      {state.confidence != null && Number.isFinite(state.confidence) ? (
        <span className={styles.meta} data-confidence={String(state.confidence)}>
          {Math.round(state.confidence * 100)}%
        </span>
      ) : null}
      {children}
    </button>
  );
}
