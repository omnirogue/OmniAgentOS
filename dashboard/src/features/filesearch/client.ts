/** Fetch client for the machine-wide file-search API.
 *
 * Every request goes SAME-ORIGIN through the Next.js proxy
 * (app/api/[...path]/route.ts). The FastAPI filesearch routes are
 * session-token-gated (`_require_token` in omniagentos/api/routes/filesearch.py),
 * so `filesearch` is registered in the proxy's AUTHORIZED_READ_PREFIXES — the
 * proxy attaches the local session token server-side and the browser never
 * holds it. Without that allowlist entry every GET here 401s. The reveal POST
 * is carried by the proxy's authorized-mutation handler (token injected the
 * same way); a dedicated route mirrors the C0 pattern at
 * app/api/filesearch/reveal/route.ts.
 */

import { fetchWithTimeout } from "../../lib/fetchTimeout";
import type {
  FileCategory,
  FileHit,
  FileRevealRequest,
  FileRoot,
  FileSearchErrorBody,
  SearchMode,
  SortKey,
} from "./types";

export class FileSearchApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "FileSearchApiError";
  }
}

/** True when the backend indexer/route simply isn't deployed on this build yet
 * — a 404 on the search endpoints. Drives the "not yet deployed" EmptyState. */
export function isNotDeployed(reason: unknown): boolean {
  return reason instanceof FileSearchApiError && reason.status === 404;
}

function detailFrom(body: FileSearchErrorBody, fallback: string): string {
  if (body && typeof body.error === "object" && body.error?.message) return body.error.message;
  if (typeof body?.error === "string") return body.message ?? body.error;
  return body?.message ?? fallback;
}

function qs(params: Record<string, string | number | undefined>): string {
  const entries = Object.entries(params).filter(
    ([, v]) => v !== "" && v !== undefined && v !== null,
  );
  return entries.length
    ? `?${new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString()}`
    : "";
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetchWithTimeout(path, { cache: "no-store" });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = detailFrom((await res.json()) as FileSearchErrorBody, detail);
    } catch {
      /* keep statusText */
    }
    throw new FileSearchApiError(detail, res.status);
  }
  return res.json() as Promise<T>;
}

/** Pull the hit array out of whatever envelope the backend settled on. The
 * extended contract is unsettled across the parallel backend branch, so we
 * accept `rows` (the semantic contract's word), `hits` (the current route's
 * word), `results`, `data`, or a bare top-level array. */
function normalizeHits(payload: unknown): FileHit[] {
  if (Array.isArray(payload)) return payload as FileHit[];
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    for (const key of ["rows", "hits", "results", "data"]) {
      const value = record[key];
      if (Array.isArray(value)) return value as FileHit[];
    }
  }
  return [];
}

export type FileSearchParams = {
  q: string;
  mode: SearchMode;
  /** undefined = "All" (param omitted). */
  root?: FileRoot;
  category?: FileCategory;
  /** Name-mode only; ignored by the score-ranked semantic endpoint. */
  sort?: SortKey;
  limit?: number;
};

export async function searchFiles(params: FileSearchParams): Promise<FileHit[]> {
  const { q, mode, root, category, sort, limit = 50 } = params;
  const query = qs({
    q,
    root,
    category,
    // Semantic results are score-ranked server-side; sort only applies to name mode.
    sort: mode === "name" ? sort : undefined,
    limit,
  });
  const base = mode === "semantic" ? "/api/filesearch/semantic" : "/api/filesearch";
  const payload = await getJson<unknown>(`${base}${query}`);
  return normalizeHits(payload);
}

/** Reveal an indexed hit in Finder. The body carries the row's own `{path,
 * root}` (never an operator-typed path); the backend re-resolves it against the
 * live catalog and refuses anything not present in the index. */
export async function revealFile(request: FileRevealRequest): Promise<void> {
  const res = await fetchWithTimeout("/api/filesearch/reveal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    body: JSON.stringify({ app: "finder", ...request }),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = detailFrom((await res.json()) as FileSearchErrorBody, detail);
    } catch {
      /* keep statusText */
    }
    throw new FileSearchApiError(detail, res.status);
  }
}
