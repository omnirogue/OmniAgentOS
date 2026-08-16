"use client";

import { Badge, Card, EmptyState, Pill } from "../../design";
import type { Episode, Fact, KnowledgeStats } from "./types";

function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function PromotedFeed({
  stats,
  facts = [],
  episodes = [],
}: {
  stats: KnowledgeStats;
  facts?: Fact[];
  episodes?: Episode[];
}) {
  const episodeById = new Map(episodes.map((episode) => [episode.id, episode]));
  const promoted = facts.filter((fact) => fact.status === "active").sort((a, b) => (b.recorded_at ?? "").localeCompare(a.recorded_at ?? ""));

  if (!promoted.length) {
    return (
      <EmptyState
        title="No promoted facts yet"
        message={
          stats.facts.active === 0
            ? "New facts will appear here after they become active."
            : "Loading promoted facts from the knowledge base…"
        }
      />
    );
  }

  return (
    <div aria-label="Recently promoted facts" style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)", maxHeight: 440, overflowY: "auto", paddingRight: "var(--space-1)" }}>
      {promoted.map((fact) => {
        const episode = fact.episode_id ? episodeById.get(fact.episode_id) : undefined;
        return (
          <Card key={fact.id} padding="sm" style={{ borderLeft: "3px solid var(--promote)" }}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: "var(--space-3)", alignItems: "flex-start" }}>
              <p style={{ margin: 0, color: "var(--text)", fontWeight: 600 }}>{fact.statement}</p>
              <Badge tone="promote">Active</Badge>
            </div>
            <p style={{ margin: "var(--space-2) 0", color: "var(--text-muted)", fontSize: "var(--text-small)" }}>
              {episode ? (
                <>
                  From <strong>{episode.source}</strong> · {episode.source_ref}
                </>
              ) : (
                "Promoted from an unavailable episode"
              )}
            </p>
            <p style={{ margin: "0 0 var(--space-2)", color: "var(--text-muted)", fontSize: "var(--text-small)" }}>
              Active — provenance: <strong>{fact.provenance}</strong> · trust {percent(fact.trust)} · confidence {percent(fact.confidence)}.
            </p>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-2)" }}>
              <Pill tone="neutral">{fact.provenance}</Pill>
              <Pill tone="ok">Trust {percent(fact.trust)}</Pill>
              <Pill tone="accent">Importance {percent(fact.importance)}</Pill>
            </div>
          </Card>
        );
      })}
    </div>
  );
}
