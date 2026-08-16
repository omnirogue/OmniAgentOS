import type { NextRequest } from "next/server";
import { proxyAuthorizedPost } from "@/lib/serverProxy";

/**
 * C0: dedicated same-origin proxy for POST /api/board/{id}/files/reveal (open a
 * card's workspace locally — Reveal in Finder / Open in an app). Mirrors
 * `api/sessions/[id]/kill/route.ts`: the catch-all `[...path]` proxy authorizes
 * GET read prefixes, so a mutation gets its own handler that reads the local
 * session token server-side and injects it — the browser never holds the token.
 */
export async function POST(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  return proxyAuthorizedPost(`/api/board/${encodeURIComponent(id)}/files/reveal`, request);
}
