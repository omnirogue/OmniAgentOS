/**
 * Typed fetch client for the H2 lab API vault routes (contracts/lab-api.md):
 *   GET /api/lab/vault/tree         -> vault note tree (folders -> notes: id,type,title,links)
 *   GET /api/lab/vault/note?path=   -> {frontmatter, body, links[]}
 *   GET /api/lab/vault/search?q=    -> [{path,title,type,snippet}]
 *
 * The lab API backend integrates on a separate branch and its exact JSON shape may not
 * match this terse contract row byte-for-byte. Every response is parsed defensively at
 * this boundary (normalize* below) into the clean types in types.ts — malformed/partial
 * fields degrade instead of throwing, so the UI never crashes on a shape drift; it just
 * renders what it can and treats the rest as absent (Loading -> Empty -> Error, never a
 * dead end). Path handling is safe by construction: fetchVaultNote refuses to build a
 * request for any path that fails isSafeVaultPath (never a `..` traversal).
 */

import { API_BASE } from "../../lib/contracts";
import { fetchWithTimeout, FetchTimeoutError } from "../../lib/fetchTimeout";
import { isSafeVaultPath } from "./paths";
import { NOTE_TYPES, type AnyNoteType, type Confidence, type VaultFrontmatter, type VaultNoteDetail, type VaultSearchHit, type VaultSearchResponse, type VaultTreeNote } from "./types";

export class VaultApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "VaultApiError";
    this.status = status;
  }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function toStringOrNull(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

function toNoteType(v: unknown): AnyNoteType {
  return typeof v === "string" && (NOTE_TYPES as readonly string[]).includes(v) ? (v as AnyNoteType) : "unknown";
}

function toConfidence(v: unknown): Confidence | null {
  return v === "low" || v === "medium" || v === "high" ? v : null;
}

function coerceLinks(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.filter((x): x is string => typeof x === "string" && x.trim().length > 0);
}

/**
 * Fetch JSON AND keep the `Response` around, so a caller that needs a
 * response header (e.g. searchVault's truncation signal) is not stuck: a
 * bare `getJson()` that only returns the parsed body throws every header
 * away, which is exactly how a real truncation signal went missing here
 * once already.
 */
async function getJsonWithResponse(path: string): Promise<{ body: unknown; response: Response }> {
  let res: Response;
  try {
    res = await fetchWithTimeout(`${API_BASE}${path}`, { cache: "no-store", headers: { Accept: "application/json" } });
  } catch (reason) {
    if (reason instanceof FetchTimeoutError) {
      throw new VaultApiError("The lab API did not respond in time — it may be down or overloaded.", 0);
    }
    throw new VaultApiError("Could not reach the lab API.", 0);
  }
  if (!res.ok) {
    let message = res.statusText || `HTTP ${res.status}`;
    try {
      const body: unknown = await res.json();
      if (isRecord(body) && isRecord(body.error) && typeof body.error.message === "string") {
        message = body.error.message;
      }
    } catch {
      /* body wasn't JSON — keep statusText */
    }
    throw new VaultApiError(message, res.status);
  }
  try {
    return { body: await res.json(), response: res };
  } catch {
    throw new VaultApiError("The lab API returned a response that was not valid JSON.", res.status);
  }
}

async function getJson(path: string): Promise<unknown> {
  return (await getJsonWithResponse(path)).body;
}

// ---------------------------------------------------------------------------
// tree
// ---------------------------------------------------------------------------

function coerceTreeNote(rec: Record<string, unknown>, fallbackPath: string | null): VaultTreeNote | null {
  const path = toStringOrNull(rec.path) ?? fallbackPath ?? toStringOrNull(rec.id);
  const id = toStringOrNull(rec.id) ?? path;
  if (!path || !id) return null;
  return {
    id,
    path,
    type: toNoteType(rec.type),
    title: toStringOrNull(rec.title) ?? id,
    links: coerceLinks(rec.links ?? rec.wikilinks ?? rec.outlinks),
    discipline: toStringOrNull(rec.discipline),
  };
}

/** Recursively walks a plausibly-nested tree/folder/array shape into a flat note list. */
function walkTree(node: unknown, acc: VaultTreeNote[], seen: Set<string>, folderPrefix: string): void {
  if (Array.isArray(node)) {
    for (const item of node) walkTree(item, acc, seen, folderPrefix);
    return;
  }
  if (!isRecord(node)) return;

  const nestedNotes = node.notes;
  const nestedFolders = node.folders ?? node.children;
  const nestedItems = node.items;
  const looksLikeFolder = Array.isArray(nestedNotes) || Array.isArray(nestedFolders) || Array.isArray(nestedItems);

  if (looksLikeFolder) {
    const name = toStringOrNull(node.name) ?? toStringOrNull(node.folder) ?? "";
    const prefix = name ? `${folderPrefix}${name}/` : folderPrefix;
    if (Array.isArray(nestedNotes)) for (const n of nestedNotes) walkTree(n, acc, seen, prefix);
    if (Array.isArray(nestedFolders)) for (const f of nestedFolders) walkTree(f, acc, seen, prefix);
    if (Array.isArray(nestedItems)) for (const it of nestedItems) walkTree(it, acc, seen, prefix);
    return;
  }

  // note-shaped leaf
  if (typeof node.id === "string" || typeof node.path === "string" || typeof node.title === "string") {
    const note = coerceTreeNote(node, folderPrefix || null);
    if (note && !seen.has(note.path)) {
      seen.add(note.path);
      acc.push(note);
    }
    return;
  }

  // unknown wrapper object — tolerate common wrapper keys
  if (node.root !== undefined) walkTree(node.root, acc, seen, folderPrefix);
  if (node.tree !== undefined) walkTree(node.tree, acc, seen, folderPrefix);
  if (node.results !== undefined) walkTree(node.results, acc, seen, folderPrefix);
}

export function normalizeTree(raw: unknown): VaultTreeNote[] {
  const acc: VaultTreeNote[] = [];
  walkTree(raw, acc, new Set(), "");
  return acc;
}

export async function fetchVaultTree(): Promise<VaultTreeNote[]> {
  const raw = await getJson("/api/lab/vault/tree");
  return normalizeTree(raw);
}

// ---------------------------------------------------------------------------
// note
// ---------------------------------------------------------------------------

function toFrontmatter(v: unknown, fallbackId: string): VaultFrontmatter {
  const rec = isRecord(v) ? v : {};
  return {
    id: toStringOrNull(rec.id) ?? fallbackId,
    type: toNoteType(rec.type),
    discipline: toStringOrNull(rec.discipline),
    created: toStringOrNull(rec.created),
    source_run: toStringOrNull(rec.source_run),
    confidence: toConfidence(rec.confidence),
    status: toStringOrNull(rec.status) ?? "active",
    supersedes: toStringOrNull(rec.supersedes),
  };
}

export function normalizeNoteDetail(raw: unknown, fallbackId: string): VaultNoteDetail {
  const rec = isRecord(raw) ? raw : {};
  const body = toStringOrNull(rec.body) ?? toStringOrNull(rec.content) ?? "";
  const frontmatter = toFrontmatter(rec.frontmatter ?? rec.frontMatter ?? rec.meta, fallbackId);
  const links = coerceLinks(rec.links ?? rec.wikilinks ?? rec.outlinks);
  return { frontmatter, body, links };
}

export async function fetchVaultNote(path: string): Promise<VaultNoteDetail> {
  if (!isSafeVaultPath(path)) {
    throw new VaultApiError("Refused to request an unsafe vault path.", 400);
  }
  const raw = await getJson(`/api/lab/vault/note?path=${encodeURIComponent(path)}`);
  return normalizeNoteDetail(raw, path);
}

// ---------------------------------------------------------------------------
// search
// ---------------------------------------------------------------------------

export function normalizeSearchHits(raw: unknown): VaultSearchHit[] {
  const arr: unknown[] = Array.isArray(raw)
    ? raw
    : isRecord(raw) && Array.isArray(raw.results)
      ? raw.results
      : isRecord(raw) && Array.isArray(raw.hits)
        ? raw.hits
        : [];
  const out: VaultSearchHit[] = [];
  for (const item of arr) {
    if (!isRecord(item)) continue;
    const path = toStringOrNull(item.path);
    if (!path) continue;
    out.push({
      path,
      title: toStringOrNull(item.title) ?? path,
      type: toNoteType(item.type),
      snippet: toStringOrNull(item.snippet) ?? "",
    });
  }
  return out;
}

export async function searchVault(query: string): Promise<VaultSearchResponse> {
  const q = query.trim();
  if (!q) return { hits: [], truncated: false };
  const { body, response } = await getJsonWithResponse(`/api/lab/vault/search?q=${encodeURIComponent(q)}`);
  return {
    hits: normalizeSearchHits(body),
    // Same-origin proxy relays this verbatim (see serverProxy.ts's relayHeaders
    // allowlist) from the FastAPI vault_search route.
    truncated: response.headers.get("X-Vault-Search-Truncated") === "true",
  };
}
