import { Select } from "@/design";
import styles from "./health.module.css";
import { STATUS_LABEL } from "./StatusBadge";
import { BROKEN_FIRST_ORDER } from "./logic";
import type { HealthFiltersState } from "./types";

const COMPANY_OPTIONS = [
  { value: "", label: "All companies" },
  { value: "omniagentos", label: "OmniAgentOS" },
  { value: "initech", label: "Initech" },
  { value: "globex", label: "Globex" },
  { value: "acmeuni", label: "AcmeUni" },
  { value: "hooli", label: "Hooli" },
  { value: "estate", label: "Estate" },
];

const KIND_OPTIONS = [
  { value: "", label: "All kinds" },
  { value: "mechanical-automation", label: "Mechanical automation" },
  { value: "llm-loop", label: "LLM loop" },
  { value: "external-service", label: "External service" },
  { value: "data-store", label: "Data store" },
  { value: "human-process", label: "Human process" },
];

const STATUS_OPTIONS = [
  { value: "", label: "All statuses" },
  ...BROKEN_FIRST_ORDER.map((status) => ({ value: status, label: STATUS_LABEL[status] })),
];

export type HealthFiltersProps = {
  filters: HealthFiltersState;
  onChange: (filters: HealthFiltersState) => void;
};

/** Three independent selects that compose (AND) via the parent's
 * filterCapabilities call — each onChange only ever updates its own
 * dimension, leaving the other two untouched. */
export function HealthFilters({ filters, onChange }: HealthFiltersProps) {
  return (
    <div className={styles.filterRow}>
      <div className={styles.filterField}>
        <Select
          label="Company"
          aria-label="Filter by company"
          value={filters.company}
          options={COMPANY_OPTIONS}
          onChange={(value) => onChange({ ...filters, company: value as HealthFiltersState["company"] })}
        />
      </div>
      <div className={styles.filterField}>
        <Select
          label="Kind"
          aria-label="Filter by kind"
          value={filters.kind}
          options={KIND_OPTIONS}
          onChange={(value) => onChange({ ...filters, kind: value as HealthFiltersState["kind"] })}
        />
      </div>
      <div className={styles.filterField}>
        <Select
          label="Status"
          aria-label="Filter by status"
          value={filters.status}
          options={STATUS_OPTIONS}
          onChange={(value) => onChange({ ...filters, status: value as HealthFiltersState["status"] })}
        />
      </div>
    </div>
  );
}
