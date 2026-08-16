import type { NextRequest } from "next/server";
import { proxyAuthorizedPost } from "@/lib/serverProxy";

/**
 * Dedicated same-origin proxy for POST /api/filesearch/reveal (Reveal in Finder
 * from a /files search hit). Mirrors `api/board/[id]/files/reveal/route.ts`: the
 * catch-all `[...path]` proxy authorizes GET read prefixes, so a mutation gets
 * its own handler that reads the local session token server-side and injects it
 * — the browser never holds the token.
 *
 * CONTRACT (for the backend branch to adopt): the request body carries the
 * hit's own `{ path, root, app }`. The backend MUST re-resolve `path` against
 * the live filesearch catalog and refuse anything not present in the index —
 * paths are server-resolved from index rows, never trusted as client-arbitrary.
 * Until the backend route exists this proxies through to a 404, which the UI
 * surfaces as "local reveal isn't deployed on this build yet".
 */
export async function POST(request: NextRequest) {
  return proxyAuthorizedPost("/api/filesearch/reveal", request);
}
