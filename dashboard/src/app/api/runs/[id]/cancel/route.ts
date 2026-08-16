import type { NextRequest } from "next/server";
import { proxyAuthorized } from "@/lib/serverProxy";

/**
 * fix6: same-origin proxy for POST /api/runs/{id}/cancel. The FastAPI route is
 * now token-gated; the dashboard's run-cancel control reaches it here so the
 * local session token is attached server-side.
 */
export async function POST(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  return proxyAuthorized(`/api/runs/${encodeURIComponent(id)}/cancel`, request, "POST");
}
