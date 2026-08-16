"use client";

import { useEffect, useState } from "react";
import { Badge, Card, ErrorState, Loading } from "@/design";
import { orgdimsApi, type PortfolioView as PortfolioData } from "./api";

export function OrgPortfolioView() {
  const [data, setData] = useState<PortfolioData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    void orgdimsApi
      .portfolio()
      .then(setData)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : "Failed to load portfolio"))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, []);

  if (loading && !data) return <Loading label="Loading portfolio…" />;
  if (error) return <ErrorState message={error} onRetry={load} />;
  if (!data) return null;

  return (
    <div style={{ display: "grid", gap: "var(--space-4)" }}>
      <p style={{ color: "var(--text-muted)" }}>
        Primary orchestrator: <Badge tone="ok">{data.primary_orchestrator}</Badge> · Grok-preferred
        cards: <Badge tone="neutral">{data.grok_preferred_cards}</Badge>
      </p>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-4)" }}>
        <Card>
          <h3 style={{ marginTop: 0 }}>By workstream</h3>
          <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {Object.entries(data.by_workstream)
              .sort((a, b) => (b[1].total ?? 0) - (a[1].total ?? 0))
              .map(([ws, counts]) => (
                <li
                  key={ws}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    padding: "6px 0",
                    borderBottom: "1px solid var(--border-subtle)",
                  }}
                >
                  <span>{ws}</span>
                  <span>
                    <Badge tone="neutral">{counts.total ?? 0}</Badge>{" "}
                    <Badge tone="running">{counts.in_progress ?? 0} live</Badge>{" "}
                    <Badge tone="warn">{counts.blocked ?? 0} blocked</Badge>
                  </span>
                </li>
              ))}
          </ul>
        </Card>
        <Card>
          <h3 style={{ marginTop: 0 }}>By company</h3>
          <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {Object.entries(data.by_company).map(([co, counts]) => (
              <li
                key={co}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  padding: "6px 0",
                  borderBottom: "1px solid var(--border-subtle)",
                }}
              >
                <span>{co}</span>
                <span>
                  <Badge tone="neutral">{counts.total ?? 0}</Badge>{" "}
                  <Badge tone="completed">{counts.done ?? 0} done</Badge>
                </span>
              </li>
            ))}
          </ul>
        </Card>
      </div>
      <Card>
        <h3 style={{ marginTop: 0 }}>Risk distribution</h3>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {Object.entries(data.by_risk).map(([risk, n]) => (
            <Badge
              key={risk}
              tone={risk === "irreversible" ? "danger" : risk.includes("external") ? "warn" : "neutral"}
            >
              {risk}: {n}
            </Badge>
          ))}
        </div>
      </Card>
    </div>
  );
}
