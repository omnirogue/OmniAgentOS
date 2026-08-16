"use client";

import { Table, type TableColumn } from "@/design";
import { statusRank } from "./logic";
import { StatusBadge } from "./StatusBadge";
import styles from "./health.module.css";
import type { CapabilityHealth } from "./types";

function formatTimestamp(iso: string | null): string {
  if (!iso) return "never";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return parsed.toLocaleString();
}

function formatMetric(metric: Record<string, unknown> | null): string {
  if (!metric) return "—";
  return Object.entries(metric)
    .map(([key, value]) => `${key}: ${String(value)}`)
    .join(", ");
}

export type HealthTableProps = {
  capabilities: CapabilityHealth[];
  onSelect: (capability: CapabilityHealth) => void;
};

/**
 * The Table primitive's own column sort is available (status/last-checked/
 * company/name, per the brief), but the ROWS this component receives are
 * already broken-first sorted by the page before any render — so the
 * default view (sortKey unset) shows broken-first with zero clicks, and a
 * user-picked column sort is additive on top of that, never a replacement
 * for it.
 */
export function HealthTable({ capabilities, onSelect }: HealthTableProps) {
  const columns: TableColumn<CapabilityHealth>[] = [
    {
      key: "name",
      header: "Name",
      sortable: true,
      sortValue: (row) => row.name,
      render: (row) => (
        <button type="button" className="ds-table__sort" onClick={() => onSelect(row)} aria-label={`View details for ${row.name}`}>
          {row.name}
        </button>
      ),
    },
    { key: "company", header: "Company", sortable: true, sortValue: (row) => row.company, render: (row) => row.company },
    { key: "kind", header: "Kind", sortable: true, sortValue: (row) => row.kind, render: (row) => row.kind },
    {
      key: "status",
      header: "Status",
      sortable: true,
      sortValue: (row) => statusRank(row.status),
      render: (row) => <StatusBadge status={row.status} />,
    },
    {
      key: "last_checked",
      header: "Last checked",
      sortable: true,
      sortValue: (row) => row.last_checked ?? "",
      render: (row) => formatTimestamp(row.last_checked),
    },
    {
      key: "last_good",
      header: "Last good",
      sortable: true,
      sortValue: (row) => row.last_good ?? "",
      render: (row) => formatTimestamp(row.last_good),
    },
    { key: "metric", header: "Key metric", render: (row) => <span className={styles.muted}>{formatMetric(row.metric)}</span> },
    { key: "owner", header: "Owner", sortable: true, sortValue: (row) => row.owner, render: (row) => row.owner },
  ];

  return (
    <Table
      columns={columns}
      rows={capabilities}
      rowKey={(row) => row.id}
      emptyMessage="No capabilities match the current filters."
      caption="Capability health — broken-first by default"
    />
  );
}
