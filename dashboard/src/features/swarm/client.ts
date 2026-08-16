/**
 * Fetch client for the Swarm fleet endpoint. C1 targets `GET /api/swarm`
 * (WP6a) but that route 404s until it merges, so `fetchSwarmFleet` resolves to
 * `null` on 404 and the page falls back to the board-derived grouping. Every
 * call goes same-origin through the Next proxy (`apiUrl`) exactly like the
 * accounts/collab clients.
 *
 * Terminals (`GET /api/sessions`, transcript) and provider health
 * (`GET /api/accounts`) are read through the EXISTING `lib/api` and
 * `features/accounts/client` — this file adds only the not-yet-built fleet read.
 */
import { apiUrl } from "@/lib/apiRoute";
import { API_BASE } from "@/lib/contracts";
import { fetchWithTimeout } from "@/lib/fetchTimeout";
import type { ProviderHealthRow, SwarmFleet, SwarmOverview, SwarmTeam } from "./types";

export class SwarmApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "SwarmApiError";
  }
}

/**
 * `GET /api/swarm`. Returns `null` when the endpoint is not yet available
 * (404) — the expected condition until WP6a — so the caller degrades to the
 * board-derived fleet grouping instead of surfacing an error. Any other
 * non-OK status throws `SwarmApiError` so a real outage is still visible.
 */
export async function fetchSwarmFleet(): Promise<SwarmFleet | null> {
  let res: Response;
  try {
    res = await fetchWithTimeout(apiUrl(API_BASE, "/api/swarm", "GET"), {
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });
  } catch {
    // Network/timeout — treat as "fleet API unavailable" and fall back.
    return null;
  }
  if (res.status === 404) return null;
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { error?: { message?: string } };
      detail = body?.error?.message ?? detail;
    } catch {
      // keep statusText
    }
    throw new SwarmApiError(`${res.status}: ${detail}`, res.status);
  }
  const body = (await res.json()) as Partial<SwarmFleet> | SwarmFleet;
  return {
    runs: Array.isArray(body?.runs) ? body.runs : [],
    utilization: body?.utilization ?? null,
  };
}

/**
 * `GET /api/swarm/overview` (C2). Fleet-level metric rollup for the throughput
 * tiles + header. Returns `null` on 404 (endpoint absent) or a network/timeout,
 * exactly like `fetchSwarmFleet`, so the panel degrades to its EmptyState
 * instead of erroring. An empty fleet is a normal `200` with `active: 0`.
 */
export async function fetchSwarmOverview(): Promise<SwarmOverview | null> {
  let res: Response;
  try {
    res = await fetchWithTimeout(apiUrl(API_BASE, "/api/swarm/overview", "GET"), {
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });
  } catch {
    return null;
  }
  if (res.status === 404) return null;
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { error?: { message?: string } };
      detail = body?.error?.message ?? detail;
    } catch {
      // keep statusText
    }
    throw new SwarmApiError(`${res.status}: ${detail}`, res.status);
  }
  return (await res.json()) as SwarmOverview;
}

/**
 * `GET /api/swarm/team` — live agents, leaders/formations, recent spawns.
 * Returns null on 404/network so the TeamPanel degrades cleanly.
 */
export async function fetchSwarmTeam(): Promise<SwarmTeam | null> {
  let res: Response;
  try {
    res = await fetchWithTimeout(apiUrl(API_BASE, "/api/swarm/team", "GET"), {
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });
  } catch {
    return null;
  }
  if (res.status === 404) return null;
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { error?: { message?: string } };
      detail = body?.error?.message ?? detail;
    } catch {
      // keep statusText
    }
    throw new SwarmApiError(`${res.status}: ${detail}`, res.status);
  }
  return (await res.json()) as SwarmTeam;
}

/**
 * `GET /api/swarm/providers` (C4). Durable per-provider/account rate-limit +
 * cooldown + inflight health — the ProviderHealthPanel's primary source.
 * Returns `null` on a 404 (route absent) or a network/timeout, which is the
 * caller's signal to fall back to `GET /api/accounts` (the plan doc's C4
 * bullet). Any OTHER non-OK status throws `SwarmApiError` so a real outage
 * stays visible rather than silently degrading.
 */
export async function fetchSwarmProviders(): Promise<ProviderHealthRow[] | null> {
  let res: Response;
  try {
    res = await fetchWithTimeout(apiUrl(API_BASE, "/api/swarm/providers", "GET"), {
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });
  } catch {
    return null;
  }
  if (res.status === 404) return null;
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { error?: { message?: string } };
      detail = body?.error?.message ?? detail;
    } catch {
      // keep statusText
    }
    throw new SwarmApiError(`${res.status}: ${detail}`, res.status);
  }
  const body = (await res.json()) as unknown;
  return Array.isArray(body) ? (body as ProviderHealthRow[]) : [];
}

/** `GET /api/swarm/{id}` — run detail (run row + member tasks + attempts).
 * Null on 404 (run pruned) so cards degrade to board-derived stats. */
export async function fetchSwarmRun(runId: string): Promise<import("./types").SwarmRunDetail | null> {
  let res: Response;
  try {
    res = await fetchWithTimeout(apiUrl(API_BASE, `/api/swarm/${encodeURIComponent(runId)}`, "GET"), {
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });
  } catch {
    return null;
  }
  if (res.status === 404) return null;
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { error?: { message?: string } };
      detail = body?.error?.message ?? detail;
    } catch {
      // keep statusText
    }
    throw new SwarmApiError(`${res.status}: ${detail}`, res.status);
  }
  return (await res.json()) as import("./types").SwarmRunDetail;
}

/** `POST /api/swarm/{id}/cancel` — pause/stop a whole run (kills live member
 * sessions; the board's run card exposes it as Pause).
 *
 * Returns the parsed cancel result. A 200 does NOT mean every session is
 * dead: `kill_complete: false` is a PARTIAL cancel (sessions still pending
 * supervisor confirmation, failed, refused as unowned, mid-spawn attempts,
 * or an attempt scan error) and callers must surface it — never a plain
 * "Run paused" success. A 200 whose body cannot be parsed returns `null`,
 * which callers should treat as "outcome unknown", not as success. */
export async function cancelSwarmRun(runId: string): Promise<import("./types").SwarmCancelResult | null> {
  const res = await fetchWithTimeout(apiUrl(API_BASE, `/api/swarm/${encodeURIComponent(runId)}/cancel`, "POST"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { error?: { message?: string } };
      detail = body?.error?.message ?? detail;
    } catch {
      // keep statusText
    }
    throw new SwarmApiError(`${res.status}: ${detail}`, res.status);
  }
  try {
    const body = (await res.json()) as import("./types").SwarmCancelResult;
    return isSwarmCancelResult(body) ? body : null;
  } catch {
    return null;
  }
}

/** F006: a 200 whose body is missing ANY required field of the declared
 * cancel contract (`id`, `status`, `sessions`, `kill_complete`) must render
 * as "outcome unknown", never as a successful "Run paused" -- a body with
 * `kill_complete` but no `sessions` (or vice versa) is exactly the shape a
 * silently-broken backend contract would produce, and treating it as valid
 * would hide a partial cancel behind a false success. */
function isSwarmCancelResult(body: unknown): body is import("./types").SwarmCancelResult {
  if (!body || typeof body !== "object") return false;
  const candidate = body as Record<string, unknown>;
  if (typeof candidate.id !== "string") return false;
  if (typeof candidate.status !== "string") return false;
  if (typeof candidate.kill_complete !== "boolean") return false;
  if (!candidate.sessions || typeof candidate.sessions !== "object") return false;
  const sessions = candidate.sessions as Record<string, unknown>;
  return (
    Array.isArray(sessions.cancelled) &&
    Array.isArray(sessions.already_terminal) &&
    Array.isArray(sessions.kill_pending) &&
    Array.isArray(sessions.failed) &&
    Array.isArray(sessions.not_owned) &&
    Array.isArray(sessions.unbound_attempts)
  );
}

/** The ids `cancelSwarmRun` reported as NOT verifiably stopped, in display
 * order: failed, refused-as-unowned, death-unconfirmed, mid-spawn attempts. */
export function unstoppedCancelIds(result: import("./types").SwarmCancelResult): string[] {
  const sessions = result.sessions;
  return [
    ...(sessions?.failed ?? []).map((item) => item.session_id),
    ...(sessions?.not_owned ?? []).map((item) => item.session_id),
    ...(sessions?.kill_pending ?? []),
    ...(sessions?.unbound_attempts ?? []),
  ];
}
