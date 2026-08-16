"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Card, EmptyState, Loading, Pill, Select, Stat } from "../../design";
import { fetchRecalls } from "./api";
import type { Fact, RecallLogEntry } from "./types";

export function RecallReplay({
  recalls: initialRecalls = [],
  facts = [],
}: {
  recalls?: RecallLogEntry[];
  facts?: Fact[];
}) {
  const [recalls, setRecalls] = useState<RecallLogEntry[]>(initialRecalls);
  const [runId, setRunId] = useState(initialRecalls[0]?.run_id ?? "");
  const [loading, setLoading] = useState(!initialRecalls.length);
  const [error, setError] = useState<string | null>(null);

  const loadRecalls = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchRecalls("");
      setRecalls(data);
      if (data.length > 0) {
        setRunId(data[0].run_id);
      }
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "Failed to load recalls";
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!initialRecalls.length && recalls.length === 0) {
      void loadRecalls();
    }
  }, [initialRecalls.length, recalls.length, loadRecalls]);

  const recall = useMemo(() => recalls.find((entry) => entry.run_id === runId) ?? null, [recalls, runId]);
  const firedFacts = useMemo(
    () =>
      recall
        ? recall.fact_ids.map((id) => facts.find((fact) => fact.id === id)).filter((fact): fact is Fact => Boolean(fact))
        : [],
    [facts, recall],
  );

  if (loading) {
    return <Loading label="Loading recall replay…" />;
  }

  if (error) {
    return <EmptyState icon={undefined} title="Failed to load recalls" message={error} />;
  }

  if (!recalls.length) {
    return <EmptyState title="No recall activity yet" message="Recall replays will appear after an agent completes a run." />;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
      <Select label="Run ID" value={runId} onChange={setRunId} options={recalls.map((entry) => ({ value: entry.run_id, label: `${entry.run_id} · ${entry.discipline}` }))} />
      {recall ? (
        <>
          <Card padding="sm">
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(9rem, 1fr))", gap: "var(--space-3)" }}>
              <Stat label="Tokens used" value={recall.tokens} />
              <Stat label="Latency" value={`${recall.latency_ms} ms`} />
              <Stat label="Facts fired" value={firedFacts.length > 0 ? firedFacts.length : recall.fact_ids.length} />
              <Stat label="Query digest" value={recall.query_digest} />
            </div>
          </Card>
          {firedFacts.length > 0 ? (
            <div aria-label="Facts recalled for selected run" style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
              {firedFacts.map((fact, index) => (
                <Card key={fact.id} padding="sm">
                  <div style={{ display: "flex", gap: "var(--space-3)", alignItems: "flex-start", justifyContent: "space-between" }}>
                    <div>
                      <strong>
                        #{index + 1} · {fact.statement}
                      </strong>
                      <p style={{ margin: "var(--space-1) 0 0", color: "var(--text-muted)", fontSize: "var(--text-small)" }}>
                        {fact.discipline} · {Math.round(fact.trust * 100)}% trust · {Math.round(fact.importance * 100)}% importance
                      </p>
                    </div>
                    <Pill tone="accent">Score {(1 - index * 0.08).toFixed(2)}</Pill>
                  </div>
                </Card>
              ))}
            </div>
          ) : (
            <EmptyState title="Fact details not available" message={`${recall.fact_ids.length} facts were recalled, but their details are not currently available. The recall was successful with ${recall.tokens} tokens and ${recall.latency_ms}ms latency.`} />
          )}
        </>
      ) : null}
    </div>
  );
}
