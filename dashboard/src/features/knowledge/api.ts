/** Defensive fetch client for the frozen Knowledge API. UI code receives only clean types. */
import { apiUrl } from "../../lib/apiRoute";
import { API_BASE } from "../../lib/contracts";
import { fetchWithTimeout, FetchTimeoutError } from "../../lib/fetchTimeout";
import {
  EDGE_TYPES,
  EPISODE_SOURCES,
  FACT_PROVENANCE,
  FACT_SCOPES,
  FACT_STATUSES,
  type Episode,
  type Fact,
  type FactDetailResponse,
  type FactSearchResult,
  type KnowledgeEdge,
  type KnowledgeGraphNode,
  type KnowledgeGraphResponse,
  type KnowledgeSearchResponse,
  type KnowledgeStats,
  type RecallLogEntry,
  type RecallPreviewRequest,
  type RecallPreviewResponse,
} from "./types";

export class KnowledgeApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "KnowledgeApiError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function text(value: unknown, fallback = ""): string { return typeof value === "string" ? value : fallback; }
function nullableText(value: unknown): string | null { return typeof value === "string" && value ? value : null; }
function number(value: unknown, fallback = 0): number { return typeof value === "number" && Number.isFinite(value) ? value : fallback; }
function integer(value: unknown, fallback = 0): number { return Number.isInteger(value) ? (value as number) : fallback; }
function bounded(value: unknown): number { return Math.max(0, Math.min(1, number(value))); }
function oneOf<T extends readonly string[]>(value: unknown, values: T, fallback: T[number]): T[number] {
  return typeof value === "string" && values.includes(value) ? (value as T[number]) : fallback;
}

async function getJson(path: string): Promise<unknown> {
  let response: Response;
  try {
    response = await fetchWithTimeout(`${API_BASE}${path}`, { cache: "no-store", headers: { Accept: "application/json" } });
  } catch (reason) {
    if (reason instanceof FetchTimeoutError) {
      throw new KnowledgeApiError("The knowledge API did not respond in time — it may be down or overloaded.", 0);
    }
    throw new KnowledgeApiError("Could not reach the knowledge API.", 0);
  }
  if (!response.ok) {
    let message = response.statusText || `HTTP ${response.status}`;
    try {
      const body: unknown = await response.json();
      if (isRecord(body) && isRecord(body.error) && typeof body.error.message === "string") message = body.error.message;
    } catch { /* Retain the response status text. */ }
    throw new KnowledgeApiError(message, response.status);
  }
  try { return await response.json(); }
  catch { throw new KnowledgeApiError("The knowledge API returned invalid JSON.", response.status); }
}

function normalizeFact(value: unknown): Fact {
  const rec = isRecord(value) ? value : {};
  return {
    id: integer(rec.id), statement: text(rec.statement), discipline: text(rec.discipline),
    scope: oneOf(rec.scope, FACT_SCOPES, "global"), episode_id: Number.isInteger(rec.episode_id) ? (rec.episode_id as number) : null,
    provenance: oneOf(rec.provenance, FACT_PROVENANCE, "ambiguous"), trust: bounded(rec.trust), confidence: bounded(rec.confidence),
    status: oneOf(rec.status, FACT_STATUSES, "quarantined"), valid_at: nullableText(rec.valid_at), recorded_at: nullableText(rec.recorded_at),
    invalid_at: nullableText(rec.invalid_at), superseded_by: Number.isInteger(rec.superseded_by) ? (rec.superseded_by as number) : null,
    importance: bounded(rec.importance), access_count: Math.max(0, integer(rec.access_count)), last_accessed: nullableText(rec.last_accessed), helped_count: Math.max(0, integer(rec.helped_count)),
  };
}

function normalizeEpisode(value: unknown): Episode | null {
  if (!isRecord(value)) return null;
  return { id: integer(value.id), source: oneOf(value.source, EPISODE_SOURCES, "run"), source_ref: text(value.source_ref), agent_id: nullableText(value.agent_id), discipline: text(value.discipline), content: text(value.content), occurred_at: nullableText(value.occurred_at) };
}

function normalizeEdge(value: unknown): KnowledgeEdge | null {
  if (!isRecord(value) || !Number.isInteger(value.id)) return null;
  const source = nullableText(value.source);
  const target = nullableText(value.target);
  if (!source || !target) return null;
  return { id: value.id as number, source, target, type: oneOf(value.type, EDGE_TYPES, "about"), weight: bounded(value.weight) };
}

function normalizeFactSearchResult(value: unknown): FactSearchResult | null {
  if (!isRecord(value) || !isRecord(value.fact)) return null;
  const signals = Array.isArray(value.signals) ? value.signals.filter((signal): signal is string => typeof signal === "string") : [];
  return { fact: normalizeFact(value.fact), score: bounded(value.score), signals };
}

export function normalizeStats(value: unknown): KnowledgeStats {
  const rec = isRecord(value) ? value : {};
  const facts = isRecord(rec.facts) ? rec.facts : {};
  const recalls = isRecord(rec.recalls) ? rec.recalls : {};
  const health = isRecord(rec.health) ? rec.health : {};
  return {
    facts: { total: Math.max(0, integer(facts.total)), active: Math.max(0, integer(facts.active)), quarantined: Math.max(0, integer(facts.quarantined)), superseded: Math.max(0, integer(facts.superseded)) },
    entities: Math.max(0, integer(rec.entities)), edges: Math.max(0, integer(rec.edges)), episodes: Math.max(0, integer(rec.episodes)),
    recalls: { count: Math.max(0, integer(recalls.count)), avg_latency_ms: Math.max(0, number(recalls.avg_latency_ms)), avg_tokens: Math.max(0, number(recalls.avg_tokens)) },
    health: { last_successful_recall_at: nullableText(health.last_successful_recall_at), recall_errors_24h: Math.max(0, integer(health.recall_errors_24h)), last_error: nullableText(health.last_error) },
    available: rec.available === true,
  };
}

export function normalizeGraph(value: unknown): KnowledgeGraphResponse {
  const rec = isRecord(value) ? value : {};
  const nodes: KnowledgeGraphNode[] = [];
  for (const raw of Array.isArray(rec.nodes) ? rec.nodes : []) {
    if (!isRecord(raw)) continue;
    const id = nullableText(raw.id);
    if (!id || (raw.kind !== "fact" && raw.kind !== "entity")) continue;
    if (raw.kind === "fact" && /^fact:\d+$/.test(id)) {
      nodes.push({ id: id as `fact:${number}`, kind: "fact", label: text(raw.label), status: oneOf(raw.status, FACT_STATUSES, "quarantined"), discipline: text(raw.discipline), importance: bounded(raw.importance), trust: bounded(raw.trust) });
    } else if (raw.kind === "entity" && /^entity:\d+$/.test(id)) {
      nodes.push({ id: id as `entity:${number}`, kind: "entity", label: text(raw.label), entityKind: text(raw.entityKind) });
    }
  }
  const edges = (Array.isArray(rec.edges) ? rec.edges : []).map(normalizeEdge).filter((edge): edge is KnowledgeEdge => edge !== null);
  return { nodes, edges };
}

export function normalizeFactDetail(value: unknown): FactDetailResponse {
  const rec = isRecord(value) ? value : {};
  const edges = (Array.isArray(rec.edges) ? rec.edges : []).map(normalizeEdge).filter((edge): edge is KnowledgeEdge => edge !== null);
  return { fact: normalizeFact(rec.fact), episode: normalizeEpisode(rec.episode), edges, superseded_by: isRecord(rec.superseded_by) ? normalizeFact(rec.superseded_by) : null };
}

export function normalizeSearch(value: unknown): KnowledgeSearchResponse {
  const rec = isRecord(value) ? value : {};
  return { results: (Array.isArray(rec.results) ? rec.results : []).map(normalizeFactSearchResult).filter((result): result is FactSearchResult => result !== null) };
}

export function normalizeRecallPreview(value: unknown): RecallPreviewResponse {
  const rec = isRecord(value) ? value : {};
  return { facts: (Array.isArray(rec.facts) ? rec.facts : []).map(normalizeFactSearchResult).filter((result): result is FactSearchResult => result !== null), rendered: text(rec.rendered), tokens: Math.max(0, integer(rec.tokens)), latency_ms: Math.max(0, number(rec.latency_ms)) };
}

export function normalizeRecalls(value: unknown): RecallLogEntry[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isRecord).map((rec) => ({
    id: integer(rec.id), run_id: text(rec.run_id), agent_id: text(rec.agent_id), discipline: text(rec.discipline), query_digest: text(rec.query_digest).slice(0, 16),
    fact_ids: Array.isArray(rec.fact_ids) ? rec.fact_ids.filter((id): id is number => Number.isInteger(id)) : [], tokens: Math.max(0, integer(rec.tokens)), latency_ms: Math.max(0, number(rec.latency_ms)), created_at: nullableText(rec.created_at),
  })).filter((recall) => recall.id > 0 && Boolean(recall.run_id));
}

export async function fetchKnowledgeStats(): Promise<KnowledgeStats> { return normalizeStats(await getJson("/api/knowledge/stats")); }
export async function fetchKnowledgeGraph(limit = 500): Promise<KnowledgeGraphResponse> { return normalizeGraph(await getJson(`/api/knowledge/graph?limit=${Math.max(1, Math.min(500, Math.floor(limit)))}`)); }
export async function fetchFact(id: number): Promise<FactDetailResponse> { return normalizeFactDetail(await getJson(`/api/knowledge/facts/${Math.max(0, Math.floor(id))}`)); }
export async function searchKnowledge(query: string, k = 20): Promise<KnowledgeSearchResponse> { return normalizeSearch(await getJson(`/api/knowledge/search?q=${encodeURIComponent(query.trim())}&k=${Math.max(1, Math.min(100, Math.floor(k)))}`)); }
export async function previewRecall(request: RecallPreviewRequest): Promise<RecallPreviewResponse> {
  let response: Response;
  // fix7: recall-preview is a POST and now token-gated, so it goes same-origin (token proxy).
  try { response = await fetchWithTimeout(apiUrl(API_BASE, "/api/knowledge/recall-preview", "POST"), { method: "POST", cache: "no-store", headers: { Accept: "application/json", "Content-Type": "application/json" }, body: JSON.stringify(request) }); }
  catch (reason) {
    if (reason instanceof FetchTimeoutError) throw new KnowledgeApiError("The knowledge API did not respond in time — it may be down or overloaded.", 0);
    throw new KnowledgeApiError("Could not reach the knowledge API.", 0);
  }
  if (!response.ok) throw new KnowledgeApiError(response.statusText || `HTTP ${response.status}`, response.status);
  try { return normalizeRecallPreview(await response.json()); }
  catch { throw new KnowledgeApiError("The knowledge API returned invalid JSON.", response.status); }
}
export async function fetchRecalls(runId: string): Promise<RecallLogEntry[]> { return normalizeRecalls(await getJson(`/api/knowledge/recalls?run_id=${encodeURIComponent(runId)}`)); }
