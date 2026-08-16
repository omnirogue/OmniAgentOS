import type { NextRequest } from "next/server";
import { proxyAuthorizedPost } from "@/lib/serverProxy";

/**
 * SEC-005: same-origin proxy for POST /api/sessions/{id}/update (title /
 * company edits made from the sessions board). Runs server-side so the
 * local session token never reaches the browser — same shape as
 * ./kill/route.ts. Exports ONLY the handler (no extra exports): the
 * required `dashboard` CI check runs `next build`, which fails a route.ts
 * that exports anything beyond handlers + config.
 */
export async function POST(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  return proxyAuthorizedPost(`/api/sessions/${encodeURIComponent(id)}/update`, request);
}
