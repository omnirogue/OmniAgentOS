"use client";

import { useMemo, useState } from "react";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  Loading,
  Select,
  Stat,
  Table,
  type TableColumn,
} from "@/design";
import { labClient } from "./client";
import { labelize, formatDate } from "./format";
import styles from "./experiments.module.css";
import { useLabData } from "./useLabData";
import type { Surface, SurfaceKind } from "./types";

const KINDS: SurfaceKind[] = [
  "prompt",
  "orchestration_genome",
  "model_assignment",
  "routing_policy",
  "review_rubric",
];

const COLUMNS: TableColumn<Surface>[] = [
  {
    key: "label",
    header: "Surface",
    sortable: true,
    sortValue: (row) => row.label,
    render: (row) => (
      <div>
        <strong>{row.label}</strong>
        <br />
        <span className={styles.muted}>{row.path}</span>
      </div>
    ),
  },
  {
    key: "kind",
    header: "Kind",
    sortable: true,
    sortValue: (row) => row.kind,
    render: (row) => <Badge tone="neutral">{labelize(row.kind)}</Badge>,
  },
  {
    key: "discipline",
    header: "Discipline",
    sortable: true,
    sortValue: (row) => row.discipline,
    render: (row) => <Badge>{row.discipline}</Badge>,
  },
  {
    key: "version",
    header: "Version",
    sortable: true,
    align: "right" as const,
    sortValue: (row) => row.version,
    render: (row) => `v${row.version}`,
  },
  {
    key: "status",
    header: "Status",
    sortable: true,
    sortValue: (row) => row.status,
    render: (row) => (
      <Badge
        category="runState"
        tone={
          row.status === "champion"
            ? "ok"
            : row.status === "challenger"
              ? "challenger"
              : row.status === "archived"
                ? "neutral"
                : "warn"
        }
      >
        {labelize(row.status)}
      </Badge>
    ),
  },
  {
    key: "created",
    header: "Created",
    sortable: true,
    sortValue: (row) => row.created_at,
    render: (row) => formatDate(row.created_at),
  },
];

export function SurfacesPanel() {
  const load = useMemo(() => () => labClient.getSurfaces(), []);
  const query = useLabData(load);
  const [discipline, setDiscipline] = useState("all");
  const [kind, setKind] = useState("all");

  const surfaces = query.status === "ready" ? query.data : [];
  const disciplines = useMemo(
    () => Array.from(new Set(surfaces.map((s) => s.discipline))).sort(),
    [surfaces],
  );

  const filtered = useMemo(
    () =>
      surfaces.filter(
        (s) =>
          (discipline === "all" || s.discipline === discipline) &&
          (kind === "all" || s.kind === kind),
      ),
    [surfaces, discipline, kind],
  );

  const championCount = filtered.filter((s) => s.status === "champion").length;
  const challengerCount = filtered.filter((s) => s.status === "challenger").length;

  return (
    <>
      <div className={styles.toolbar}>
        <div className={styles.toolbarTitle}>
          <span className={styles.sectionTitle}>Surface library</span>
          <span className={styles.muted}>
            Versioned prompts, genomes, and policies available for experiments
          </span>
        </div>
        <div className={styles.filters} aria-label="Surface filters">
          <Select
            aria-label="Filter by discipline"
            value={discipline}
            onChange={setDiscipline}
            options={[
              { value: "all", label: "All disciplines" },
              ...disciplines.map((value) => ({ value, label: labelize(value) })),
            ]}
          />
          <Select
            aria-label="Filter by kind"
            value={kind}
            onChange={setKind}
            options={[
              { value: "all", label: "All kinds" },
              ...KINDS.map((value) => ({ value, label: labelize(value) })),
            ]}
          />
        </div>
      </div>

      <div className={styles.statGrid}>
        <Stat label="Total surfaces" value={surfaces.length} />
        <Stat label="Champions" value={championCount} tone="ok" />
        <Stat label="Challengers" value={challengerCount} tone="accent" />
        <Stat label="Disciplines" value={disciplines.length} />
      </div>

      {query.status === "loading" ? <Loading variant="skeleton" lines={6} label="Loading surfaces" /> : null}
      {query.status === "error" ? (
        <ErrorState title="Surfaces unavailable" message={query.error} onRetry={query.retry} />
      ) : null}
      {query.status === "ready" && surfaces.length === 0 ? (
        <EmptyState
          title="No surfaces registered"
          message="The lab surface library is empty. Surfaces are versioned prompts, orchestration genomes, routing policies, model assignments, and review rubrics available for controlled experiments."
        />
      ) : null}
      {query.status === "ready" && filtered.length === 0 && surfaces.length > 0 ? (
        <EmptyState
          title="No matching surfaces"
          message="No surfaces match the selected discipline and kind."
        />
      ) : null}
      {query.status === "ready" && filtered.length > 0 ? (
        <Card padding="none" className={styles.tableCard}>
          <Table
            columns={COLUMNS}
            rows={filtered}
            rowKey={(row) => row.id}
            caption="Surfaces available for experiments"
          />
        </Card>
      ) : null}
    </>
  );
}
