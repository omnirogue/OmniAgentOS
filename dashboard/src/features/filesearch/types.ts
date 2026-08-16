/**
 * Wire types for the machine-wide file-search surface — the same file-finding
 * power the agents have (omniagentos/filesearch + api/routes/filesearch.py),
 * surfaced on the dashboard /files page.
 *
 * NOTE (contract in flight): this UI is built against the EXTENDED filesearch
 * contract the backend branch is landing in parallel, NOT the shape currently
 * live in omniagentos/api/routes/filesearch.py. The two differ deliberately:
 *
 *   - GET  /api/filesearch?q=&root=&category=&sort=&limit=      (name-ranked)
 *   - GET  /api/filesearch/semantic?q=&root=&category=&limit=   (score-ranked)
 *       → rows: { path, root, category, mtime, score, excerpt }
 *   - POST /api/filesearch/reveal  { path, root, app }          (see client.ts)
 *
 * The endpoints may not exist yet on this branch — the client treats a 404 as
 * "backend indexing not yet deployed" and the UI renders a graceful EmptyState.
 * The response envelope is normalized defensively (rows | hits | results | a
 * bare array), so whichever key the backend settles on, the table still fills.
 */

/** Filesystem root a hit lives under. `desktop` = the local machine home tree,
 * `repo` = the OmniAgentOS working tree. Backend param values are these exact
 * lowercase tokens; "All" in the UI simply omits the param. */
export type FileRoot = "desktop" | "icloud" | "gdrive" | "repo";

/** The nine content buckets the indexer classifies every file into. */
export type FileCategory =
  | "documents"
  | "spreadsheets"
  | "presentations"
  | "images"
  | "video"
  | "audio"
  | "code"
  | "archives"
  | "other";

export type SearchMode = "name" | "semantic";
export type SortKey = "recency" | "name";

/** One search hit. `size`/`score`/`excerpt` are optional: `size` "where
 * present" (some roots don't stat), `score`+`excerpt` only in semantic mode. */
export type FileHit = {
  path: string;
  root: FileRoot;
  category: FileCategory;
  /** Epoch SECONDS (the mounts convention — see features/mounts/types.ts).
   * Multiply by 1000 before `Date`. May be absent for un-statted roots. */
  mtime?: number | null;
  size?: number | null;
  /** Semantic relevance, higher = closer. Absent in name mode. */
  score?: number | null;
  /** Best-matching content snippet. Absent in name mode. */
  excerpt?: string | null;
};

/** Reveal an indexed hit locally (Reveal in Finder). Server-resolved: the
 * backend re-validates `{path, root}` against the live catalog and refuses any
 * path not present in the index — the client never names an arbitrary path.
 * `app` mirrors the C0 board reveal contract; defaults to "finder". */
export type FileRevealApp = "finder";
export type FileRevealRequest = {
  path: string;
  root: FileRoot;
  app?: FileRevealApp;
};

/** Structured error envelope from `omniagentos.api.services.ApiError`, matching
 * the mounts surface (features/mounts/types.ts). The flatter `{error, message}`
 * shape is also tolerated — `FileSearchApiError` in ./client normalizes both. */
export type FileSearchErrorBody = {
  error?: { code?: string; message?: string; details?: unknown } | string;
  message?: string;
};

export const FILE_ROOTS: ReadonlyArray<{ value: FileRoot; label: string }> = [
  { value: "desktop", label: "Desktop" },
  { value: "icloud", label: "iCloud" },
  { value: "gdrive", label: "Google Drive" },
  { value: "repo", label: "Repo" },
];

export const FILE_CATEGORIES: ReadonlyArray<{ value: FileCategory; label: string }> = [
  { value: "documents", label: "Documents" },
  { value: "spreadsheets", label: "Spreadsheets" },
  { value: "presentations", label: "Presentations" },
  { value: "images", label: "Images" },
  { value: "video", label: "Video" },
  { value: "audio", label: "Audio" },
  { value: "code", label: "Code" },
  { value: "archives", label: "Archives" },
  { value: "other", label: "Other" },
];

const ROOT_LABELS: Record<FileRoot, string> = Object.fromEntries(
  FILE_ROOTS.map((r) => [r.value, r.label]),
) as Record<FileRoot, string>;

const CATEGORY_LABELS: Record<FileCategory, string> = Object.fromEntries(
  FILE_CATEGORIES.map((c) => [c.value, c.label]),
) as Record<FileCategory, string>;

export function rootLabel(root: FileRoot): string {
  return ROOT_LABELS[root] ?? root;
}

export function categoryLabel(category: FileCategory): string {
  return CATEGORY_LABELS[category] ?? category;
}
