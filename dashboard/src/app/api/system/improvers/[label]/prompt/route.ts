import type { NextRequest } from "next/server";
import { proxyAuthorized } from "@/lib/serverProxy";

/**
 * Dedicated same-origin proxy for PATCH /api/system/improvers/{label}/prompt
 * (kill-route pattern). Overwrites the improver's editable prompt file and commits
 * it to the OmniAgentOS repo audit trail; it runs server-side so the local session
 * token never reaches the browser. FastAPI resolves the target path from launchd
 * discovery (never from the client body), requires the file to exist, and caps the
 * content at 64KB (see omniagentos/api/routes/system.py).
 */
export async function PATCH(request: NextRequest, context: { params: Promise<{ label: string }> }) {
  const { label } = await context.params;
  return proxyAuthorized(
    `/api/system/improvers/${encodeURIComponent(label)}/prompt`,
    request,
    "PATCH",
  );
}
