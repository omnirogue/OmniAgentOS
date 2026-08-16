import type { NextRequest } from "next/server";
import { proxyAuthorized, proxyPublicRead } from "@/lib/serverProxy";
import { tasksUpstreamPath } from "./upstreamPath";

/**
 * fix6 / C-09: same-origin proxy for /api/tasks.
 *
 * POST is token-gated (voice-dispatch create): session token is injected
 * server-side and never exposed to the browser.
 *
 * GET must also be declared here. A more-specific route shadows the catch-all
 * for every method on this path — a POST-only handler 405s Activity and
 * silently empties project counts. GET stays a public read (no token), matching
 * the catch-all's proxyPublicRead path for `tasks`.
 *
 * Query string must be forwarded: Activity and project hierarchy call
 * GET /api/tasks?project_id=… / ?state=…; dropping searchParams empties counts.
 */
export async function POST(request: NextRequest) {
  return proxyAuthorized(tasksUpstreamPath(request, "POST"), request, "POST");
}

export async function GET(request: NextRequest) {
  return proxyPublicRead(tasksUpstreamPath(request, "GET"), request);
}
