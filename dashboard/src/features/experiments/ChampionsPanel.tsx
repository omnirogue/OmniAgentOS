"use client";

import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Loading,
  Select,
  Stat,
} from "@/design";
import { labClient } from "./client";
import { labelize, formatDate } from "./format";
import styles from "./experiments.module.css";
import { useLabData } from "./useLabData";
import type { Champion, SurfaceKind } from "./types";

const KIND_LABELS: Record<SurfaceKind, string> = {
  prompt: "Prompt",
  orchestration_genome: "Genome",
  model_assignment: "Model",
  routing_policy: "Routing",
  review_rubric: "Rubric",
};

function ChampionCard({
  champion,
  onRollback,
  rollbackBusy,
}: {
  champion: Champion;
  onRollback: (c: Champion) => void;
  rollbackBusy: boolean;
}) {
  const canRollback = champion.rollback_to_surface_id != null;
  return (
    <Card>
      <div className={styles.cardHeading}>
        <div>
          <Badge tone="ok">{KIND_LABELS[champion.surface_kind] ?? champion.surface_kind}</Badge>
          <Badge tone="neutral">{champion.discipline}</Badge>
        </div>
        {canRollback ? (
          <Button
            size="sm"
            variant="ghost"
            disabled={rollbackBusy}
            onClick={() => onRollback(champion)}
          >
            Rollback
          </Button>
        ) : null}
      </div>

      <div className={styles.mono}>v{champion.surface_version} · {champion.surface_id}</div>

      <div className={styles.muted}>
        Promoted {formatDate(champion.promoted_at)}
        {champion.promoted_from_experiment
          ? ` from ${champion.promoted_from_experiment}`
          : null}
      </div>

      {champion.history && champion.history.length > 0 ? (
        <div>
          <span className={styles.eyebrow}>History ({champion.history.length})</span>
          <ul className={styles.timeline}>
            {champion.history.map((entry, index) => (
              <li key={`${entry.event}-${entry.promoted_at}-${index}`}>
                <span className={styles.timelineDot} />
                <div>
                  <strong>{entry.event}</strong>{" "}
                  <span className={styles.mono}>v{entry.surface_version ?? "?"}</span>
                  <p>{formatDate(entry.promoted_at)}</p>
                </div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  );
}

export function ChampionsPanel() {
  const load = useMemo(() => () => labClient.getChampions(), []);
  const query = useLabData(load);
  const [discipline, setDiscipline] = useState("all");

  const champions = query.status === "ready" ? query.data : [];
  const disciplines = useMemo(
    () => Array.from(new Set(champions.map((c) => c.discipline))).sort(),
    [champions],
  );

  const filtered = useMemo(
    () =>
      champions.filter((c) => discipline === "all" || c.discipline === discipline),
    [champions, discipline],
  );

  const [rollbackMsg, setRollbackMsg] = useState<string | null>(null);
  const [rollbackBusy, setRollbackBusy] = useState(false);

  const handleRollback = async (c: Champion) => {
    setRollbackBusy(true);
    setRollbackMsg(null);
    try {
      await labClient.rollbackChampion(c.discipline, c.surface_kind);
      setRollbackMsg(`Rollback queued for ${c.discipline} / ${c.surface_kind}`);
    } catch (err) {
      setRollbackMsg(err instanceof Error ? err.message : "Rollback failed");
    } finally {
      setRollbackBusy(false);
    }
  };

  return (
    <>
      <div className={styles.toolbar}>
        <div className={styles.toolbarTitle}>
          <span className={styles.sectionTitle}>Champions</span>
          <span className={styles.muted}>
            Currently promoted surfaces per discipline — the baseline every challenger must beat
          </span>
        </div>
        <div className={styles.filters} aria-label="Champion filters">
          <Select
            aria-label="Filter by discipline"
            value={discipline}
            onChange={setDiscipline}
            options={[
              { value: "all", label: "All disciplines" },
              ...disciplines.map((value) => ({ value, label: labelize(value) })),
            ]}
          />
        </div>
      </div>

      <div className={styles.statGrid}>
        <Stat label="Champion disciplines" value={champions.length} />
        <Stat label="With rollback" value={champions.filter((c) => c.rollback_to_surface_id).length} tone="accent" />
      </div>

      {rollbackMsg ? (
        <div className={rollbackMsg.startsWith("Rollback queued") ? styles.actionSuccess : styles.actionError}>
          {rollbackMsg}
        </div>
      ) : null}

      {query.status === "loading" ? <Loading variant="skeleton" lines={6} label="Loading champions" /> : null}
      {query.status === "error" ? (
        <ErrorState title="Champions unavailable" message={query.error} onRetry={query.retry} />
      ) : null}
      {query.status === "ready" && champions.length === 0 ? (
        <EmptyState
          title="No champions registered"
          message="Champions are the currently promoted surface per discipline — prompts, genomes, policies, and rubrics that experiments must beat. They appear here after a successful experiment disposition."
        />
      ) : null}
      {query.status === "ready" && filtered.length > 0 ? (
        <div className={styles.lowerGrid}>
          {filtered.map((c) => (
            <ChampionCard
              key={`${c.discipline}-${c.surface_kind}`}
              champion={c}
              onRollback={handleRollback}
              rollbackBusy={rollbackBusy}
            />
          ))}
        </div>
      ) : null}
    </>
  );
}
