import type { NextRequest } from "next/server";
import { proxyAuthorized } from "@/lib/serverProxy";

/**
 * fix6: same-origin proxy for POST /api/tasks/{id}/runs. The FastAPI route is
 * now token-gated; the dashboard's voice-dispatch create-run flow reaches it
 * through here so the local session token stays server-side.
 */
export async function POST(request: NextRequest, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  return proxyAuthorized(`/api/tasks/${encodeURIComponent(id)}/runs`, request, "POST");
}
