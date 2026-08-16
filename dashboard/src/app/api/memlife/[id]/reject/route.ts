import type { NextRequest } from "next/server";
import { proxyAuthorizedPost } from "@/lib/serverProxy";

/**
 * SEC-005: same-origin proxy for POST /api/memlife/{id}/reject.
 * Session token is injected server-side — browser never holds it.
 */
export async function POST(
  request: NextRequest,
  context: { params: Promise<{ id: string }> },
) {
  const { id } = await context.params;
  return proxyAuthorizedPost(
    `/api/memlife/${encodeURIComponent(id)}/reject`,
    request,
  );
}
