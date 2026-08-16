/** Synthetic API-shaped fixtures. The /knowledge page intentionally uses these until p5. */
import type { Episode, Fact, KnowledgeGraphResponse, KnowledgeStats, RecallLogEntry } from "./types";

export const HEALTHY_KNOWLEDGE_STATS: KnowledgeStats = {
  facts: { total: 342, active: 305, quarantined: 12, superseded: 25 },
  entities: 89, edges: 451, episodes: 23,
  recalls: { count: 156, avg_latency_ms: 45, avg_tokens: 800 },
  health: { last_successful_recall_at: "2026-07-12T13:45:00Z", recall_errors_24h: 0, last_error: null },
  available: true,
};

export const DEGRADED_KNOWLEDGE_STATS: KnowledgeStats = {
  ...HEALTHY_KNOWLEDGE_STATS,
  available: false,
  health: { last_successful_recall_at: "2026-07-11T08:12:00Z", recall_errors_24h: 3, last_error: "Recall index is temporarily unavailable." },
};

export const KNOWLEDGE_FACTS: Fact[] = [
  { id: 12, statement: "Reasoning quality improves when complex tasks are decomposed into explicit intermediate checks.", discipline: "reasoning", scope: "global", episode_id: 5, provenance: "extracted", trust: 0.92, confidence: 0.85, status: "active", valid_at: "2026-07-08T10:00:00Z", recorded_at: "2026-07-08T10:05:00Z", invalid_at: null, superseded_by: null, importance: 0.8, access_count: 45, last_accessed: "2026-07-12T13:45:00Z", helped_count: 12 },
  { id: 45, statement: "A concise output schema reduces verbose preambles in research briefs.", discipline: "research-briefs", scope: "discipline", episode_id: 7, provenance: "extracted", trust: 0.89, confidence: 0.82, status: "active", valid_at: "2026-07-10T08:00:00Z", recorded_at: "2026-07-10T08:08:00Z", invalid_at: null, superseded_by: null, importance: 0.76, access_count: 31, last_accessed: "2026-07-12T13:42:00Z", helped_count: 9 },
  { id: 61, statement: "State assumptions before proposing an irreversible operational action.", discipline: "operations", scope: "global", episode_id: 9, provenance: "inferred", trust: 0.95, confidence: 0.9, status: "active", valid_at: "2026-07-11T14:20:00Z", recorded_at: "2026-07-11T14:25:00Z", invalid_at: null, superseded_by: null, importance: 0.91, access_count: 22, last_accessed: "2026-07-12T12:30:00Z", helped_count: 8 },
  { id: 74, statement: "Retrying a failed tool call without changing inputs rarely improves the outcome.", discipline: "operations", scope: "agent", episode_id: 10, provenance: "extracted", trust: 0.87, confidence: 0.78, status: "active", valid_at: "2026-07-11T16:00:00Z", recorded_at: "2026-07-11T16:04:00Z", invalid_at: null, superseded_by: null, importance: 0.68, access_count: 16, last_accessed: "2026-07-12T11:12:00Z", helped_count: 5 },
  { id: 83, statement: "Evidence from a single short benchmark is insufficient for a global promotion.", discipline: "evaluation", scope: "global", episode_id: 12, provenance: "ambiguous", trust: 0.56, confidence: 0.51, status: "quarantined", valid_at: "2026-07-09T09:00:00Z", recorded_at: "2026-07-09T09:03:00Z", invalid_at: null, superseded_by: null, importance: 0.43, access_count: 3, last_accessed: null, helped_count: 0 },
  { id: 91, statement: "Chain-of-thought prompts always improve reasoning regardless of task complexity.", discipline: "reasoning", scope: "global", episode_id: 5, provenance: "extracted", trust: 0.4, confidence: 0.4, status: "superseded", valid_at: "2026-07-08T10:00:00Z", recorded_at: "2026-07-08T10:06:00Z", invalid_at: "2026-07-11T14:25:00Z", superseded_by: 12, importance: 0.21, access_count: 7, last_accessed: "2026-07-10T09:00:00Z", helped_count: 1 },
  { id: 108, statement: "A retrieval budget near 800 tokens preserves useful context without obscuring the task.", discipline: "reasoning", scope: "global", episode_id: 14, provenance: "inferred", trust: 0.9, confidence: 0.84, status: "active", valid_at: "2026-07-12T09:20:00Z", recorded_at: "2026-07-12T09:26:00Z", invalid_at: null, superseded_by: null, importance: 0.72, access_count: 11, last_accessed: "2026-07-12T13:45:00Z", helped_count: 4 },
];

export const KNOWLEDGE_EPISODES: Episode[] = [
  { id: 5, source: "run", source_ref: "run-2026-07-08-001", agent_id: "claude-opus", discipline: "reasoning", content: "Experiment results showing stronger calibrated reasoning with explicit intermediate checks.", occurred_at: "2026-07-08T10:00:00Z" },
  { id: 7, source: "vault", source_ref: "experiments/brief-summarizer-v2.md", agent_id: null, discipline: "research-briefs", content: "Judges preferred concise brief introductions with a fixed output schema.", occurred_at: "2026-07-10T08:00:00Z" },
  { id: 9, source: "human", source_ref: "operator-note-2026-07-11", agent_id: null, discipline: "operations", content: "Operator review established a reversible-first policy for impactful changes.", occurred_at: "2026-07-11T14:20:00Z" },
  { id: 10, source: "run", source_ref: "run-2026-07-11-008", agent_id: "claude-opus", discipline: "operations", content: "Repeated retries succeeded only after an input or environment change.", occurred_at: "2026-07-11T16:00:00Z" },
  { id: 14, source: "chat", source_ref: "chat-2026-07-12-003", agent_id: "claude-opus", discipline: "reasoning", content: "Recall preview showed focused context at an 800 token budget.", occurred_at: "2026-07-12T09:20:00Z" },
];

export const KNOWLEDGE_GRAPH: KnowledgeGraphResponse = {
  nodes: [
    ...KNOWLEDGE_FACTS.map((fact) => ({ id: `fact:${fact.id}` as const, kind: "fact" as const, label: fact.statement, status: fact.status, discipline: fact.discipline, importance: fact.importance, trust: fact.trust })),
    { id: "entity:3", kind: "entity", label: "Chain-of-Thought", entityKind: "technique" },
    { id: "entity:8", kind: "entity", label: "Output schema", entityKind: "artifact" },
    { id: "entity:11", kind: "entity", label: "Recall budget", entityKind: "policy" },
    { id: "entity:15", kind: "entity", label: "Operational safety", entityKind: "concept" },
  ],
  edges: [
    { id: 1, source: "fact:12", target: "entity:3", type: "about", weight: 0.8 }, { id: 2, source: "fact:45", target: "entity:8", type: "about", weight: 0.76 },
    { id: 3, source: "fact:61", target: "entity:15", type: "about", weight: 0.91 }, { id: 4, source: "fact:74", target: "entity:15", type: "follows", weight: 0.68 },
    { id: 5, source: "fact:108", target: "entity:11", type: "about", weight: 0.72 }, { id: 6, source: "fact:91", target: "fact:12", type: "refines", weight: 0.76 },
    { id: 7, source: "fact:83", target: "fact:61", type: "contradicts", weight: 0.52 }, { id: 8, source: "fact:12", target: "fact:108", type: "co_occurs", weight: 0.65 },
    { id: 9, source: "fact:45", target: "fact:12", type: "same_run", weight: 0.41 }, { id: 10, source: "fact:61", target: "fact:74", type: "causes", weight: 0.6 },
    { id: 11, source: "fact:108", target: "fact:12", type: "derived_from", weight: 0.7 }, { id: 12, source: "fact:83", target: "entity:15", type: "about", weight: 0.43 },
  ],
};

export const KNOWLEDGE_RECALLS: RecallLogEntry[] = [
  { id: 1, run_id: "run-2026-07-12-001", agent_id: "claude-opus", discipline: "reasoning", query_digest: "a1b2c3d4e5f6a7b8", fact_ids: [12, 108, 61], tokens: 650, latency_ms: 48, created_at: "2026-07-12T13:45:00Z" },
  { id: 2, run_id: "run-2026-07-12-004", agent_id: "claude-opus", discipline: "research-briefs", query_digest: "81d4e0a5cbd1392f", fact_ids: [45, 12], tokens: 780, latency_ms: 44, created_at: "2026-07-12T12:32:00Z" },
  { id: 3, run_id: "run-2026-07-11-008", agent_id: "claude-opus", discipline: "operations", query_digest: "c77f13a0b584d9e2", fact_ids: [61, 74], tokens: 710, latency_ms: 52, created_at: "2026-07-11T16:05:00Z" },
];

export function fixtureSearch(query: string): Fact[] {
  const terms = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
  return KNOWLEDGE_FACTS.filter((fact) => terms.some((term) => `${fact.statement} ${fact.discipline}`.toLowerCase().includes(term)));
}
