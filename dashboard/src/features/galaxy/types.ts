/**
 * Vault Browser / Memory Galaxy — data types.
 *
 * Mirrors contracts/lab-api.md (vault/tree, vault/note, vault/search) and the frozen
 * 8-field VaultFrontmatter (omniagentos/contracts.py). The lab API backend integrates on
 * a separate branch, so response shapes are normalized defensively at the API boundary
 * (see api.ts) into these clean types — the UI never trusts raw JSON shape directly.
 */

/** The 12 frozen NoteType values (omniagentos.contracts.NoteType). */
export const NOTE_TYPES = [
  "run",
  "experiment",
  "learning",
  "failure",
  "decision",
  "benchmark",
  "discipline",
  "source",
  "tournament",
  "leaderboard",
  "playbook",
  "prompt",
] as const;

export type NoteType = (typeof NOTE_TYPES)[number];

/** UI-only widening for defensive parsing — never sent over the wire. */
export type AnyNoteType = NoteType | "unknown";

export type Confidence = "low" | "medium" | "high";

/** The frozen 8-field vault frontmatter (contracts/vault-frontmatter.md). */
export interface VaultFrontmatter {
  id: string;
  type: AnyNoteType;
  discipline: string | null;
  created: string | null;
  source_run: string | null;
  confidence: Confidence | null;
  status: string;
  supersedes: string | null;
}

/** One note as it appears in GET /api/lab/vault/tree (folders → notes: id,type,title,links). */
export interface VaultTreeNote {
  id: string;
  /** Vault-relative path, confined to the vault dir. Used verbatim for vault/note?path=. */
  path: string;
  type: AnyNoteType;
  title: string;
  /** Raw wikilink targets found in this note's body (unresolved). */
  links: string[];
  discipline: string | null;
}

/** GET /api/lab/vault/note?path= response. */
export interface VaultNoteDetail {
  frontmatter: VaultFrontmatter;
  body: string;
  links: string[];
}

/** One row of GET /api/lab/vault/search?q=. */
export interface VaultSearchHit {
  path: string;
  title: string;
  type: AnyNoteType;
  snippet: string;
}

/**
 * GET /api/lab/vault/search?q= result, with the truncation signal carried
 * on the backend's `X-Vault-Search-Truncated` response header. `truncated`
 * means the result MAY be incomplete (the backend's candidate-scan cap or
 * result-limit cap was hit) -- it must not be silently dropped, or an
 * incomplete search reads as a complete one to the person searching.
 */
export interface VaultSearchResponse {
  hits: VaultSearchHit[];
  truncated: boolean;
}

export type FetchStatus = "loading" | "ready" | "empty" | "error";
