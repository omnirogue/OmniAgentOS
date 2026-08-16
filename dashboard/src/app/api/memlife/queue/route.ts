import type { NextRequest } from "next/server";
import { proxyPublicRead } from "@/lib/serverProxy";

/**
 * Same-origin proxy for GET /api/memlife/queue.
 *
 * Relays upstream status and body verbatim so the three queue states stay
 * distinct in the browser:
 *   - 200 {pending: N}
 *   - 200 {pending: 0}
 *   - non-200 {error: store_unavailable}
 *
 * A dedicated route (rather than only the catch-all) keeps this surface
 * discoverable next to the page and prevents a more-specific future POST
 * under /api/memlife from shadowing the GET without a handler.
 */
export async function GET(request: NextRequest) {
  return proxyPublicRead("/api/memlife/queue", request);
}
