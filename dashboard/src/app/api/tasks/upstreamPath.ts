import type { NextRequest } from "next/server";

/**
 * C-09: build the upstream FastAPI path for /api/tasks.
 * GET must forward searchParams (project_id, state, limit, …).
 * POST create stays a bare path (no client query noise).
 */
export function tasksUpstreamPath(request: NextRequest, method: "GET" | "POST"): string {
  if (method === "POST") return "/api/tasks";
  return `/api/tasks${request.nextUrl.search}`;
}
