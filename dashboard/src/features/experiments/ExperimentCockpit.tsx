"use client";

import Link from "next/link";
import { useCallback, useMemo, useState } from "react";
import {
  Badge,
  BarChart,
  Button,
  Card,
  Dialog,
  EmptyState,
  ErrorState,
  Icon,
  Input,
  LineChart,
  Loading,
  Page,
  PageHeader,
  Pill,
  Stat,
  StatusDot,
} from "@/design";
import { labClient } from "./client";
import { formatDate, formatDelta, formatMoney, formatNumber, labelize, scoreMetric, statusTone } from "./format";
import styles from "./experiments.module.css";
import type {
  Arm,
  Champion,
  Disposition,
  EvalResult,
  ExperimentDetail,
  SurfaceDetail,
} from "./types";
import { useLabData } from "./useLabData";

interface CockpitData {
  experiment: ExperimentDetail;
  championSurface: SurfaceDetail;
  challengerSurface: SurfaceDetail;
  champion: Champion | null;
}

function contentText(surface: SurfaceDetail): string {
  return typeof surface.content === "string" ? surface.content : JSON.stringify(surface.content, null, 2);
}

function metricValue(result: EvalResult, metric: string): number | null {
  const value = result.metrics[metric];
  return typeof value === "number" ? value : null;
}

function cumulativeSeries(results: EvalResult[], arm: Arm, metric: string) {
  const values = results
    .filter((item) => item.arm === arm)
    .sort((a, b) => a.replicate - b.replicate)
    .map((item) => metricValue(item, metric))
    .filter((value): value is number => value != null);

  return values.map((_, index) => {
    const sample = values.slice(0, index + 1);
    const mean = sample.reduce((sum, value) => sum + value, 0) / sample.length;
    const variance = sample.length > 1
      ? sample.reduce((sum, value) => sum + (value - mean) ** 2, 0) / (sample.length - 1)
      : 0;
    const margin = sample.length > 1 ? 1.96 * Math.sqrt(variance / sample.length) : 0;
    return { x: `R${index + 1}`, y: mean, ciLow: mean - margin, ciHigh: mean + margin };
  });
}

function caseBars(results: EvalResult[], metric: string) {
  const grouped = new Map<string, { champion: number[]; challenger: number[] }>();
  results.forEach((result) => {
    Object.entries(result.per_case).forEach(([caseId, metrics]) => {
      const value = metrics[metric];
      if (typeof value !== "number") return;
      const group = grouped.get(caseId) ?? { champion: [], challenger: [] };
      group[result.arm].push(value);
      grouped.set(caseId, group);
    });
  });
  return Array.from(grouped.entries()).flatMap(([caseId, values]) => {
    const short = caseId.replace(/^case[-_]/, "");
    const average = (items: number[]) => items.reduce((sum, value) => sum + value, 0) / items.length;
    return [
      ...(values.champion.length ? [{ label: `${short} · C`, value: average(values.champion), tone: "champion" }] : []),
      ...(values.challenger.length ? [{ label: `${short} · X`, value: average(values.challenger), tone: "challenger" }] : []),
    ];
  });
}

export function ExperimentCockpit({ id }: { id: string }) {
  const load = useCallback(async (): Promise<CockpitData> => {
    const experiment = await labClient.getExperiment(id);
    const [championSurface, challengerSurface, champions] = await Promise.all([
      labClient.getSurface(experiment.champion_surface_id),
      labClient.getSurface(experiment.challenger_surface_id),
      labClient.getChampions(experiment.discipline),
    ]);
    return {
      experiment,
      championSurface,
      challengerSurface,
      champion: champions.find((entry) => entry.surface_kind === experiment.mutable_surface_kind) ?? null,
    };
  }, [id]);
  const query = useLabData(load);

  if (query.status === "loading") {
    return <Page><Loading variant="skeleton" lines={12} label="Loading experiment cockpit" /></Page>;
  }
  if (query.status === "error") {
    return <Page><ErrorState title="Experiment cockpit unavailable" message={query.error} onRetry={query.retry} action={<div className={styles.actions}><Button size="sm" variant="secondary" onClick={query.retry}>Retry</Button><Link className={styles.textLink} href="/lab">Back to experiments</Link></div>} /></Page>;
  }
  return <CockpitView data={query.data} onRefresh={query.retry} />;
}

function CockpitView({ data, onRefresh }: { data: CockpitData; onRefresh: () => void }) {
  const { experiment, championSurface, challengerSurface, champion } = data;
  const [decisionOpen, setDecisionOpen] = useState(false);
  const [rollbackOpen, setRollbackOpen] = useState(false);
  const [decision, setDecision] = useState<Disposition>("promote");
  const [decidedBy, setDecidedBy] = useState("");
  const [note, setNote] = useState("");
  const [actionState, setActionState] = useState<"idle" | "working" | "done" | "error">("idle");
  const [actionMessage, setActionMessage] = useState("");

  const scorecard = experiment.scorecard;
  const primaryMetric = experiment.primary_metric || "primary_metric";
  const lineSeries = useMemo(() => [
    { name: "Champion", color: "var(--champion)", points: cumulativeSeries(experiment.eval_results, "champion", primaryMetric) },
    { name: "Challenger", color: "var(--challenger)", points: cumulativeSeries(experiment.eval_results, "challenger", primaryMetric) },
  ], [experiment.eval_results, primaryMetric]);
  const bars = useMemo(() => caseBars(experiment.eval_results, primaryMetric), [experiment.eval_results, primaryMetric]);
  const replicateCount = Math.max(
    0,
    ...experiment.eval_results.map((item) => item.replicate + 1),
    experiment.budgets.replicates,
  );
  const reproducible = experiment.eval_results.length > 0 && experiment.eval_results.every((item) => item.deterministic_passed);
  const costChampion = scoreMetric(scorecard?.champion, ["cost_usd", "cost"]);
  const costChallenger = scoreMetric(scorecard?.challenger, ["cost_usd", "cost"]);
  const latencyChampion = scoreMetric(scorecard?.champion, ["latency_seconds", "latency_ms", "latency"]);
  const latencyChallenger = scoreMetric(scorecard?.challenger, ["latency_seconds", "latency_ms", "latency"]);
  const relative = (base: number | null, next: number | null) => base != null && next != null && base !== 0 ? (next - base) / Math.abs(base) : null;
  const ci = scorecard?.confidence_interval;

  const submitDecision = async () => {
    if (!decidedBy.trim() || !note.trim()) return;
    setActionState("working");
    try {
      await labClient.setDisposition(experiment.id, { decision, note: note.trim(), decided_by: decidedBy.trim() });
      setActionMessage(`${labelize(decision)} disposition recorded by ${decidedBy.trim()}.`);
      setActionState("done");
      setDecisionOpen(false);
      onRefresh();
    } catch (error: unknown) {
      setActionMessage(error instanceof Error ? error.message : "Disposition could not be recorded.");
      setActionState("error");
    }
  };

  const rollback = async () => {
    setActionState("working");
    try {
      const updated = await labClient.rollbackChampion(experiment.discipline, experiment.mutable_surface_kind);
      setActionMessage(`Champion rolled back to ${updated.surface_id}.`);
      setActionState("done");
      setRollbackOpen(false);
      onRefresh();
    } catch (error: unknown) {
      setActionMessage(error instanceof Error ? error.message : "Rollback failed.");
      setActionState("error");
    }
  };

  const runExperiment = async () => {
    setActionState("working");
    try {
      const response = await labClient.runExperiment(experiment.id);
      setActionMessage(`Experiment queued with status ${labelize(response.status)}.`);
      setActionState("done");
      onRefresh();
    } catch (error: unknown) {
      setActionMessage(error instanceof Error ? error.message : "Experiment could not be started.");
      setActionState("error");
    }
  };

  return (
    <Page>
      <PageHeader
        before={
          <>
            <nav className={styles.breadcrumb} aria-label="Breadcrumb"><Link href="/lab">Experiment Lab</Link><Icon name="chevronRight" size={11} /><span>{experiment.id}</span></nav>
            <div className={styles.headerBadges}><Badge tone="neutral">{experiment.discipline}</Badge><Badge tone="neutral">{labelize(experiment.mutable_surface_kind)}</Badge><span className={styles.status}><StatusDot state={statusTone(experiment.status)} /><Badge category="runState" tone={statusTone(experiment.status)}>{labelize(experiment.status)}</Badge></span></div>
          </>
        }
        title={experiment.hypothesis}
        meta={<><span className={styles.mono}>{experiment.id}</span><span>Updated {formatDate(experiment.updated_at)}</span></>}
        actions={<Button variant="secondary" size="sm" onClick={() => void runExperiment()} disabled={experiment.status.includes("running") || actionState === "working"}>Run experiment</Button>}
      />

      {actionMessage ? <div className={actionState === "error" ? styles.actionError : styles.actionSuccess} role={actionState === "error" ? "alert" : "status"}>{actionMessage}</div> : null}

      <section className={styles.armGrid} aria-label="Champion and challenger surface diff">
        <Card padding="none" className={styles.armCard} style={{ borderTopColor: "var(--champion)" }}>
          <div className={styles.armHeader}><div><Badge category="arm" tone="champion">Champion</Badge><h2>{championSurface.label || championSurface.id}</h2></div><div className={styles.armVersion}>v{championSurface.version}<span className={styles.mono}>{championSurface.content_hash}</span></div></div>
          <pre className={styles.diff}><code>{contentText(championSurface)}</code></pre>
        </Card>
        <div className={styles.versus} aria-hidden="true">VS</div>
        <Card padding="none" className={styles.armCard} style={{ borderTopColor: "var(--challenger)" }}>
          <div className={styles.armHeader}><div><Badge category="arm" tone="challenger">Challenger</Badge><h2>{challengerSurface.label || challengerSurface.id}</h2></div><div className={styles.armVersion}>v{challengerSurface.version}<span className={styles.mono}>{challengerSurface.content_hash}</span></div></div>
          <pre className={styles.diff}><code>{contentText(challengerSurface)}</code></pre>
        </Card>
      </section>

      <section aria-labelledby="scorecards-title">
        <div className={styles.sectionHeading}><div><div className={styles.eyebrow}>Decision evidence</div><h2 id="scorecards-title">Scorecards</h2></div><span className={styles.muted}>95% confidence interval where available</span></div>
        <div className={styles.statGrid}>
          <Stat label="Primary delta" value={formatDelta(scorecard?.primary_delta)} delta={ci?.length === 2 ? `CI ${formatDelta(ci[0])} to ${formatDelta(ci[1])}` : "CI unavailable"} deltaTrend={(scorecard?.primary_delta ?? 0) > 0 ? "up" : (scorecard?.primary_delta ?? 0) < 0 ? "down" : "neutral"} tone={(scorecard?.primary_delta ?? 0) > 0 ? "promote" : "reject"} />
          <Stat label="Utility" value={formatNumber(scorecard?.utility)} delta={`Floor ${formatNumber(experiment.promotion_threshold.min_utility)}`} tone={(scorecard?.utility ?? -1) >= experiment.promotion_threshold.min_utility ? "promote" : "reject"} />
          <Stat label="Complexity delta" value={formatDelta(scorecard?.complexity_delta)} delta={`Cap ${formatDelta(experiment.promotion_threshold.max_complexity_delta)}`} tone={(scorecard?.complexity_delta ?? 1) <= experiment.promotion_threshold.max_complexity_delta ? "ok" : "warn"} />
          <Stat label="Cost" value={formatMoney(costChallenger)} delta={`${formatDelta(relative(costChampion, costChallenger))} vs champion`} deltaTrend={(relative(costChampion, costChallenger) ?? 0) > 0 ? "down" : "up"} />
          <Stat label="Latency" value={latencyChallenger == null ? "—" : `${formatNumber(latencyChallenger, 1)}s`} delta={`${formatDelta(relative(latencyChampion, latencyChallenger))} vs champion`} deltaTrend={(relative(latencyChampion, latencyChallenger) ?? 0) > 0 ? "down" : "up"} />
        </div>
      </section>

      <section className={styles.chartGrid} aria-label="Experiment metrics">
        <Card>
          <div className={styles.cardHeading}><div><h2>{labelize(primaryMetric)}</h2><p>Cumulative mean by replicate with computed 95% confidence bands.</p></div><div className={styles.legend}><span><i style={{ background: "var(--champion)" }} />Champion</span><span><i style={{ background: "var(--challenger)" }} />Challenger</span></div></div>
          <LineChart series={lineSeries} status={lineSeries.every((series) => series.points.length === 0) ? "empty" : "ready"} aria-label={`${labelize(primaryMetric)} across replicates with confidence intervals`} />
        </Card>
        <Card>
          <div className={styles.cardHeading}><div><h2>Per-case results</h2><p>Mean scored metric by blinded case and arm.</p></div></div>
          <BarChart bars={bars} status={bars.length ? "ready" : "empty"} aria-label={`${labelize(primaryMetric)} per-case comparison`} />
        </Card>
      </section>

      <section className={styles.lowerGrid}>
        <Card className={styles.dispositionCard} style={{ borderTopColor: experiment.disposition ? `var(--${experiment.disposition === "human_review" ? "awaiting" : experiment.disposition === "invalid" ? "text-faint" : experiment.disposition})` : "var(--border)" }}>
          <div className={styles.cardHeading}><div><div className={styles.eyebrow}>Computed outcome</div><h2>Disposition</h2></div>{experiment.disposition ? <Badge category="disposition" tone={experiment.disposition}>{labelize(experiment.disposition)}</Badge> : <Badge tone="neutral">Pending</Badge>}</div>
          <div className={styles.reasonList}>
            {(scorecard?.audit_flags ?? []).map((flag) => <div className={styles.reason} key={flag}><span className={styles.reasonIcon}>!</span><div><strong>Audit flag</strong><p>{flag}</p></div></div>)}
            <div className={styles.reason}><span className={scorecard?.safety_regression ? styles.reasonDanger : styles.reasonOk}>{scorecard?.safety_regression ? "!" : "✓"}</span><div><strong>Safety gate</strong><p>{scorecard?.safety_regression ? "A safety regression was detected; promotion is blocked." : challengerSurface.safety_relevant ? "This surface is safety-relevant and requires a human decision." : "No safety regression was reported."}</p></div></div>
            <div className={styles.reason}><span className={reproducible ? styles.reasonOk : styles.reasonDanger}>{reproducible ? "✓" : "!"}</span><div><strong>Reproducibility</strong><p>{reproducible ? `${replicateCount} replicates passed deterministic checks.` : "Deterministic replication evidence is incomplete."}</p></div></div>
            <div className={styles.reason}><span className={champion?.rollback_to_surface_id ? styles.reasonOk : styles.reasonDanger}>{champion?.rollback_to_surface_id ? "✓" : "!"}</span><div><strong>Rollback target</strong><p>{champion?.rollback_to_surface_id ?? "No known-good rollback target returned by the registry."}</p></div></div>
          </div>
          <div className={styles.governanceNote}><strong>Human authority boundary</strong><p>Safety, review-rubric, and routing changes always require a named human approver. Numeric gates cannot override this requirement.</p></div>
          {experiment.disposition === "human_review" ? <Button onClick={() => setDecisionOpen(true)}>Review disposition</Button> : null}
        </Card>

        <Card>
          <div className={styles.cardHeading}><div><div className={styles.eyebrow}>Reversibility</div><h2>Promotion ladder</h2></div>{champion ? <Pill tone="ok">Current v{champion.surface_version}</Pill> : null}</div>
          {!champion ? <EmptyState title="No champion history" message="The registry returned no champion for this discipline and surface." /> : (
            <>
              <ol className={styles.timeline}>
                {champion.history.map((entry, index) => <li key={`${entry.surface_id}-${entry.promoted_at}`}><span className={styles.timelineDot} data-current={index === champion.history.length - 1 || undefined} /><div><div className={styles.timelineTop}><strong>v{entry.surface_version ?? index + 1} · {entry.surface_id}</strong><Badge tone={index === champion.history.length - 1 ? "champion" : "neutral"}>{index === champion.history.length - 1 ? "current" : entry.event}</Badge></div><p>{entry.decided_by ?? "campaign runner"} · {formatDate(entry.promoted_at)}</p>{entry.promoted_from_experiment ? <Link href={`/lab/${entry.promoted_from_experiment}`}>{entry.promoted_from_experiment}</Link> : null}</div></li>)}
              </ol>
              <div className={styles.rollbackBar}><div><span>Known-good target</span><strong className={styles.mono}>{champion.rollback_to_surface_id ?? "None"}</strong></div><Button variant="danger" size="sm" disabled={!champion.rollback_to_surface_id} onClick={() => setRollbackOpen(true)}>Rollback champion</Button></div>
            </>
          )}
        </Card>
      </section>

      <section aria-labelledby="provenance-title">
        <div className={styles.sectionHeading}><div><div className={styles.eyebrow}>Repeatable by construction</div><h2 id="provenance-title">Replication & provenance</h2></div></div>
        <Card className={styles.provenanceGrid}>
          <Provenance label="Replicates" value={`${replicateCount}`} hint={`${experiment.eval_results.length} arm results returned`} />
          <Provenance label="Budget" value={`${experiment.budgets.wall_minutes ?? "—"} min · ${experiment.budgets.tokens?.toLocaleString() ?? "—"} tokens`} hint={`${formatMoney(experiment.budgets.cost_usd)} ceiling`} />
          <Provenance label="Snapshot hash" value={experiment.snapshot_hash || "Not returned"} mono />
          <Provenance label="Dataset hash" value={experiment.dataset_hash || "Not returned"} mono />
          <Provenance label="Eval suite" value={experiment.eval_suite_id} mono hint={`Metric: ${primaryMetric}`} />
          <Provenance label="Surface hashes" value={`${championSurface.content_hash} · ${challengerSurface.content_hash}`} mono />
        </Card>
      </section>

      <Dialog open={decisionOpen} onClose={() => setDecisionOpen(false)} title="Human disposition review" footer={<><Button variant="ghost" onClick={() => setDecisionOpen(false)}>Cancel</Button><Button variant={decision === "reject" ? "danger" : "primary"} disabled={!decidedBy.trim() || !note.trim() || actionState === "working"} onClick={() => void submitDecision()}>Record {decision}</Button></>}>
        <div className={styles.dialogStack}><p>Record an auditable human decision. Promotion remains subject to server-side safety and rollback checks.</p><div className={styles.decisionButtons}><Button size="sm" variant={decision === "promote" ? "primary" : "secondary"} onClick={() => setDecision("promote")}>Promote</Button><Button size="sm" variant={decision === "reject" ? "danger" : "secondary"} onClick={() => setDecision("reject")}>Reject</Button></div><Input label="Decided by" placeholder="operator identity" value={decidedBy} onChange={(event) => setDecidedBy(event.target.value)} /><Input label="Decision note" placeholder="Reason and evidence considered" value={note} onChange={(event) => setNote(event.target.value)} /></div>
      </Dialog>

      <Dialog open={rollbackOpen} onClose={() => setRollbackOpen(false)} title="Rollback champion?" footer={<><Button variant="ghost" onClick={() => setRollbackOpen(false)}>Cancel</Button><Button variant="danger" disabled={actionState === "working"} onClick={() => void rollback()}>Confirm rollback</Button></>}>
        <p className={styles.dialogCopy}>This moves the {experiment.discipline} / {labelize(experiment.mutable_surface_kind)} champion pointer from <span className={styles.mono}>{champion?.surface_id}</span> to the known-good surface <span className={styles.mono}>{champion?.rollback_to_surface_id}</span>.</p>
      </Dialog>
    </Page>
  );
}

function Provenance({ label, value, hint, mono = false }: { label: string; value: string; hint?: string; mono?: boolean }) {
  return <div className={styles.provenanceItem}><span>{label}</span><strong className={mono ? styles.mono : undefined}>{value}</strong>{hint ? <small>{hint}</small> : null}</div>;
}
