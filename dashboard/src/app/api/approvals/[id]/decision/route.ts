import type { NextRequest } from "next/server";
import { proxyAuthorizedPost } from "@/lib/serverProxy";

/**
 * SEC-005: same-origin proxy for POST /api/approvals/{id}/decision. Runs
 * server-side so the local session token never reaches the browser.
 */
export async function POST(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  return proxyAuthorizedPost(`/api/approvals/${encodeURIComponent(id)}/decision`, request);
}
