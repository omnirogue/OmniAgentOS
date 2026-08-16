"use client";

import { useId } from "react";
import { Pill, Select, type SelectOption } from "@/design";
import { systemJobHealthLabel, type SystemJobHealth } from "@/features/routines/systemJobs";
import { ALL_HEALTHS, type LoopsFilterState, type LoopsSortKey } from "./filterSort";
import styles from "./loops.module.css";

const SORT_OPTIONS: SelectOption[] = [
  { value: "default", label: "Default (grouped by category)" },
  { value: "name", label: "Name" },
  { value: "lastRun", label: "Last run" },
  { value: "health", label: "Health severity" },
  { value: "schedule", label: "Schedule cadence" },
];

export type LoopsSortValue = LoopsSortKey | "default";

function toggle<T>(set: Set<T>, value: T): Set<T> {
  const next = new Set(set);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}

export function LoopsFilters({
  categories,
  filter,
  onFilterChange,
  sort,
  onSortChange,
}: {
  categories: string[];
  filter: LoopsFilterState;
  onFilterChange: (next: LoopsFilterState) => void;
  sort: LoopsSortValue;
  onSortChange: (next: LoopsSortValue) => void;
}) {
  const categoryLabelId = useId();
  const healthLabelId = useId();
  return (
    <div className={styles.controlsRow}>
      <div className={styles.controlGroup} role="group" aria-labelledby={categoryLabelId}>
        <span id={categoryLabelId} className={styles.controlLabel}>
          Category
        </span>
        {categories.map((category) => {
          const active = filter.categories.has(category);
          return (
            <button
              key={category}
              type="button"
              className={styles.chipToggle}
              aria-pressed={active}
              onClick={() =>
                onFilterChange({ ...filter, categories: toggle(filter.categories, category) })
              }
            >
              <Pill tone={active ? "accent" : "neutral"}>{category}</Pill>
            </button>
          );
        })}
      </div>
      <div className={styles.controlGroup} role="group" aria-labelledby={healthLabelId}>
        <span id={healthLabelId} className={styles.controlLabel}>
          Health
        </span>
        {ALL_HEALTHS.map((health: SystemJobHealth) => {
          const active = filter.healths.has(health);
          return (
            <button
              key={health}
              type="button"
              className={styles.chipToggle}
              aria-pressed={active}
              onClick={() => onFilterChange({ ...filter, healths: toggle(filter.healths, health) })}
            >
              <Pill tone={active ? "accent" : "neutral"}>{systemJobHealthLabel(health)}</Pill>
            </button>
          );
        })}
      </div>
      <div className={styles.controlGroup}>
        <span className={styles.controlLabel}>Sort by</span>
        <Select
          className={styles.sortSelect}
          aria-label="Sort loops by"
          options={SORT_OPTIONS}
          value={sort}
          onChange={(value) => onSortChange(value as LoopsSortValue)}
        />
      </div>
    </div>
  );
}
