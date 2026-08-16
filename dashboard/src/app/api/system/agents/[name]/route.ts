import type { NextRequest } from "next/server";
import { proxyAuthorized } from "@/lib/serverProxy";

/**
 * Dedicated same-origin proxy for PATCH /api/system/agents/{name} (kill-route
 * pattern). The mutation rewrites whitelisted frontmatter of the agent's editable
 * file and commits it to the home repo audit trail; it runs server-side so the
 * local session token never reaches the browser. FastAPI rails the write to the
 * agents directory with no path traversal (see omniagentos/api/routes/system.py).
 */
export async function PATCH(request: NextRequest, context: { params: Promise<{ name: string }> }) {
  const { name } = await context.params;
  return proxyAuthorized(`/api/system/agents/${encodeURIComponent(name)}`, request, "PATCH");
}
