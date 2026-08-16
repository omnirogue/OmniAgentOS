"use client";

import Link from "next/link";
import { useCallback, useMemo, useState } from "react";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Loading,
  Page,
  PageHeader,
  Select,
  StatusDot,
  Table,
  type TableColumn,
} from "@/design";
import { labClient, USE_FIXTURES } from "./client";
import { formatDate, formatDelta, formatNumber, labelize, scoreMetric, statusTone } from "./format";
import styles from "./experiments.module.css";
import type { Experiment, ExperimentStatus } from "./types";
import { useLabData } from "./useLabData";

const STATUSES: ExperimentStatus[] = [
  "proposed", "running_baseline", "running_challenger", "evaluating", "replicating",
  "decided", "canary", "promoted", "rolled_back", "failed", "cancelled",
];

function relativeDelta(champion: number | null, challenger: number | null): number | null {
  if (champion == null || challenger == null || champion === 0) return null;
  return (challenger - champion) / Math.abs(champion);
}

function Delta({ value, invert = false }: { value: number | null | undefined; invert?: boolean }) {
  const positive = value != null && value > 0;
  const negative = value != null && value < 0;
  const tone = invert ? (positive ? styles.deltaDown : negative ? styles.deltaUp : undefined) : positive ? styles.deltaUp : negative ? styles.deltaDown : undefined;
  return <span className={`${styles.metric} ${tone ?? ""}`}>{formatDelta(value)}</span>;
}

export function ExperimentsList() {
  const load = useCallback(() => labClient.getExperiments(), []);
  const query = useLabData(load);
  const [discipline, setDiscipline] = useState("all");
  const [status, setStatus] = useState("all");

  const experiments = useMemo(
    () => query.status === "ready" ? query.data : [],
    [query.data, query.status],
  );
  const disciplines = useMemo(
    () => Array.from(new Set(experiments.map((item) => item.discipline))).sort(),
    [experiments],
  );
  const filtered = useMemo(
    () => experiments.filter((item) =>
      (discipline === "all" || item.discipline === discipline) &&
      (status === "all" || item.status === status)),
    [discipline, experiments, status],
  );

  const columns = useMemo<TableColumn<Experiment>[]>(() => [
    {
      key: "experiment", header: "Experiment", sortable: true,
      sortValue: (row) => row.hypothesis,
      render: (row) => (
        <div className={styles.experimentCell}>
          <Link className={styles.experimentLink} href={`/lab/${row.id}`}>{row.hypothesis}</Link>
          <span className={styles.mono}>{row.id}</span>
        </div>
      ),
    },
    { key: "discipline", header: "Discipline", sortable: true, sortValue: (row) => row.discipline, render: (row) => <Badge tone="neutral">{row.discipline}</Badge> },
    { key: "surface", header: "Surface", sortable: true, sortValue: (row) => row.mutable_surface_kind, render: (row) => labelize(row.mutable_surface_kind) },
    {
      key: "status", header: "Status", sortable: true, sortValue: (row) => row.status,
      render: (row) => <span className={styles.status}><StatusDot state={statusTone(row.status)} /><Badge category="runState" tone={statusTone(row.status)}>{labelize(row.status)}</Badge></span>,
    },
    {
      key: "primary_delta", header: "Primary Δ", sortable: true, align: "right",
      sortValue: (row) => row.scorecard?.primary_delta,
      render: (row) => <Delta value={row.scorecard?.primary_delta} />,
    },
    {
      key: "disposition", header: "Disposition", sortable: true, sortValue: (row) => row.disposition,
      render: (row) => row.disposition ? <Badge category="disposition" tone={row.disposition}>{labelize(row.disposition)}</Badge> : <span className={styles.muted}>Pending</span>,
    },
    { key: "utility", header: "Utility", sortable: true, align: "right", sortValue: (row) => row.scorecard?.utility, render: (row) => <span className={styles.metric}>{formatNumber(row.scorecard?.utility)}</span> },
    {
      key: "cost", header: "Cost Δ", sortable: true, align: "right",
      sortValue: (row) => relativeDelta(scoreMetric(row.scorecard?.champion, ["cost_usd", "cost"]), scoreMetric(row.scorecard?.challenger, ["cost_usd", "cost"])),
      render: (row) => <Delta invert value={relativeDelta(scoreMetric(row.scorecard?.champion, ["cost_usd", "cost"]), scoreMetric(row.scorecard?.challenger, ["cost_usd", "cost"]))} />,
    },
    {
      key: "latency", header: "Latency Δ", sortable: true, align: "right",
      sortValue: (row) => relativeDelta(scoreMetric(row.scorecard?.champion, ["latency_seconds", "latency_ms", "latency"]), scoreMetric(row.scorecard?.challenger, ["latency_seconds", "latency_ms", "latency"])),
      render: (row) => <Delta invert value={relativeDelta(scoreMetric(row.scorecard?.champion, ["latency_seconds", "latency_ms", "latency"]), scoreMetric(row.scorecard?.challenger, ["latency_seconds", "latency_ms", "latency"]))} />,
    },
    { key: "created", header: "Created", sortable: true, sortValue: (row) => row.created_at, render: (row) => <time className={styles.date} dateTime={row.created_at}>{formatDate(row.created_at)}</time> },
  ], []);

  const clearFilters = () => { setDiscipline("all"); setStatus("all"); };

  return (
    <Page>
      <PageHeader
        eyebrow="Empirical self-improvement"
        title="Experiment Lab"
        lead="Compare controlled mutations, inspect evidence, and promote only what survives replication."
        actions={
          <>
            {USE_FIXTURES ? <Badge tone="warn">Dev fixtures</Badge> : <Badge tone="neutral">Live API</Badge>}
            <span className={styles.count}>{experiments.length} experiments</span>
          </>
        }
      />

      <Card padding="none" className={styles.tableCard}>
        <div className={styles.toolbar}>
          <div className={styles.toolbarTitle}>
            <span className={styles.sectionTitle}>Experiment ledger</span>
            <span className={styles.muted}>Newest campaigns and their final evidence</span>
          </div>
          <div className={styles.filters} aria-label="Experiment filters">
            <Select aria-label="Filter by discipline" value={discipline} onChange={setDiscipline} options={[{ value: "all", label: "All disciplines" }, ...disciplines.map((value) => ({ value, label: labelize(value) }))]} />
            <Select aria-label="Filter by status" value={status} onChange={setStatus} options={[{ value: "all", label: "All statuses" }, ...STATUSES.map((value) => ({ value, label: labelize(value) }))]} />
          </div>
        </div>

        {query.status === "loading" ? <div className={styles.statePad}><Loading variant="skeleton" lines={8} label="Loading experiments" /></div> : null}
        {query.status === "error" ? <ErrorState title="Experiment ledger unavailable" message={query.error} onRetry={query.retry} /> : null}
        {query.status === "ready" && experiments.length === 0 ? <EmptyState title="No experiments yet" message="The lab API is connected, but no campaigns have been created." /> : null}
        {query.status === "ready" && experiments.length > 0 && filtered.length === 0 ? <EmptyState title="No matching experiments" message="No experiments match the selected discipline and status." action={<Button size="sm" variant="secondary" onClick={clearFilters}>Clear filters</Button>} /> : null}
        {query.status === "ready" && filtered.length > 0 ? <Table columns={columns} rows={filtered} rowKey={(row) => row.id} caption="Experiments sorted by operator-selected column" /> : null}
      </Card>
    </Page>
  );
}
