"use client";

import { useMemo } from "react";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  Loading,
  Stat,
} from "@/design";
import { labClient } from "./client";
import { labelize, formatDate } from "./format";
import styles from "./experiments.module.css";
import { useLabData } from "./useLabData";
import type { DisciplineSummary } from "./types";

function DisciplineCard({ summary }: { summary: DisciplineSummary }) {
  const champions = summary.champion_surfaces ?? [];
  return (
    <Card>
      <div className={styles.cardHeading}>
        <div>
          <h3>{labelize(summary.discipline)}</h3>
          <p>
            {summary.experiment_count} experiment{summary.experiment_count === 1 ? "" : "s"}
            {summary.newest_at ? ` · last ${formatDate(summary.newest_at)}` : null}
          </p>
        </div>
        {summary.top_elo != null ? (
          <Badge tone="neutral" className={styles.metric}>
            ELO {Math.round(summary.top_elo)}
          </Badge>
        ) : null}
      </div>

      {champions.length > 0 ? (
        <div>
          <span className={styles.eyebrow}>Champions</span>
          {champions.map((c) => (
            <div key={`${c.discipline}-${c.surface_kind}-${c.surface_id}`}>
              <Badge tone="ok">{labelize(c.surface_kind)}</Badge>{" "}
              <span className={styles.mono}>v{c.surface_version} · {c.surface_id}</span>{" "}
              <span className={styles.muted}>promoted {formatDate(c.promoted_at)}</span>
            </div>
          ))}
        </div>
      ) : (
        <p className={styles.muted}>No champions registered for this discipline.</p>
      )}
    </Card>
  );
}

export function DisciplinesPanel() {
  const load = useMemo(() => () => labClient.getDisciplines(), []);
  const query = useLabData(load);

  const summaries = query.status === "ready" ? query.data : [];
  const totalExperiments = summaries.reduce((sum, s) => sum + s.experiment_count, 0);
  const totalChampions = summaries.reduce(
    (sum, s) => sum + (s.champion_surfaces?.length ?? 0),
    0,
  );
  const withChampions = summaries.filter((s) => (s.champion_surfaces?.length ?? 0) > 0).length;

  return (
    <>
      <div className={styles.toolbar}>
        <div className={styles.toolbarTitle}>
          <span className={styles.sectionTitle}>Discipline overview</span>
          <span className={styles.muted}>
            Per-discipline roll-up: experiment count, top ELO, and the champion surfaces available for new experiments
          </span>
        </div>
      </div>

      <div className={styles.statGrid}>
        <Stat label="Disciplines" value={summaries.length} />
        <Stat label="Total experiments" value={totalExperiments} />
        <Stat label="With champions" value={withChampions} tone="ok" />
        <Stat label="Champion surfaces" value={totalChampions} tone="accent" />
      </div>

      {query.status === "loading" ? <Loading variant="skeleton" lines={6} label="Loading disciplines" /> : null}
      {query.status === "error" ? (
        <ErrorState title="Disciplines unavailable" message={query.error} onRetry={query.retry} />
      ) : null}
      {query.status === "ready" && summaries.length === 0 ? (
        <EmptyState
          title="No disciplines found"
          message="Disciplines appear once the lab has surfaces, experiments, tournaments, or champions associated with them."
        />
      ) : null}
      {query.status === "ready" && summaries.length > 0 ? (
        <div className={styles.lowerGrid}>
          {summaries.map((s) => (
            <DisciplineCard key={s.discipline} summary={s} />
          ))}
        </div>
      ) : null}
    </>
  );
}
