/** Frozen client contract for the Knowledge / Memory Galaxy API. */

export const FACT_STATUSES = ["quarantined", "active", "superseded"] as const;
export type FactStatus = (typeof FACT_STATUSES)[number];

export const FACT_PROVENANCE = ["extracted", "inferred", "ambiguous"] as const;
export type FactProvenance = (typeof FACT_PROVENANCE)[number];

export const FACT_SCOPES = ["global", "discipline", "agent"] as const;
export type FactScope = (typeof FACT_SCOPES)[number];

export const EDGE_TYPES = ["about", "causes", "contradicts", "follows", "co_occurs", "refines", "same_run", "derived_from"] as const;
export type KnowledgeEdgeType = (typeof EDGE_TYPES)[number];

export const EPISODE_SOURCES = ["run", "web", "curator", "vault", "human", "chat"] as const;
export type EpisodeSource = (typeof EPISODE_SOURCES)[number];

export type KnowledgeNodeKind = "fact" | "entity";

export interface Fact {
  id: number;
  statement: string;
  discipline: string;
  scope: FactScope;
  episode_id: number | null;
  provenance: FactProvenance;
  trust: number;
  confidence: number;
  status: FactStatus;
  valid_at: string | null;
  recorded_at: string | null;
  invalid_at: string | null;
  superseded_by: number | null;
  importance: number;
  access_count: number;
  last_accessed: string | null;
  helped_count: number;
}

export interface Episode {
  id: number;
  source: EpisodeSource;
  source_ref: string;
  agent_id: string | null;
  discipline: string;
  content: string;
  occurred_at: string | null;
}

export interface KnowledgeGraphFactNode {
  id: `fact:${number}`;
  kind: "fact";
  label: string;
  status: FactStatus;
  discipline: string;
  importance: number;
  trust: number;
}

export interface KnowledgeGraphEntityNode {
  id: `entity:${number}`;
  kind: "entity";
  label: string;
  entityKind: string;
}

export type KnowledgeGraphNode = KnowledgeGraphFactNode | KnowledgeGraphEntityNode;

export interface KnowledgeEdge {
  id: number;
  source: string;
  target: string;
  type: KnowledgeEdgeType;
  weight: number;
}

export interface KnowledgeGraphResponse {
  nodes: KnowledgeGraphNode[];
  edges: KnowledgeEdge[];
}

export interface KnowledgeStats {
  facts: { total: number; active: number; quarantined: number; superseded: number };
  entities: number;
  edges: number;
  episodes: number;
  recalls: { count: number; avg_latency_ms: number; avg_tokens: number };
  health: { last_successful_recall_at: string | null; recall_errors_24h: number; last_error: string | null };
  available: boolean;
}

export interface FactDetailResponse {
  fact: Fact;
  episode: Episode | null;
  edges: KnowledgeEdge[];
  superseded_by: Fact | null;
}

export interface FactSearchResult {
  fact: Fact;
  score: number;
  signals: string[];
}

export interface KnowledgeSearchResponse {
  results: FactSearchResult[];
}

export interface RecallPreviewRequest {
  prompt: string;
  discipline?: string;
  agent_id?: string;
}

export interface RecallPreviewResponse {
  facts: FactSearchResult[];
  rendered: string;
  tokens: number;
  latency_ms: number;
}

export interface RecallLogEntry {
  id: number;
  run_id: string;
  agent_id: string;
  discipline: string;
  query_digest: string;
  fact_ids: number[];
  tokens: number;
  latency_ms: number;
  created_at: string | null;
}
