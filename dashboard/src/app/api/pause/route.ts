import type { NextRequest } from "next/server";
import { proxyAuthorized, proxyPublicRead } from "@/lib/serverProxy";

/**
 * fix6: same-origin proxy for PUT /api/pause. The FastAPI route is now
 * token-gated (an agent must not be able to un-pause a safety-paused system),
 * so the dashboard's pause/resume control reaches it here with the local
 * session token attached server-side.
 *
 * GET must also be handled here: this file being more specific than the
 * `api/[...path]` catch-all means Next.js resolves every method for
 * `/api/pause` to THIS route, not the catch-all — an unhandled GET 405s
 * instead of falling through. PauseControl polls GET /api/pause on every
 * page (it lives in the root layout), so the header showed a bare "405:
 * Method Not Allowed" on every load until this GET was added. It stays a
 * public read (no token), matching the catch-all's proxyPublicRead for any
 * other unauthenticated GET.
 */
export async function GET(request: NextRequest) {
  return proxyPublicRead("/api/pause", request);
}

export async function PUT(request: NextRequest) {
  return proxyAuthorized("/api/pause", request, "PUT");
}
