/**
 * SEC-005 / AC-policy fix7: the FastAPI control plane now gates EVERY mutating
 * request (POST/PUT/PATCH/DELETE) on the local session token. The browser must
 * never hold that token, so a mutation is sent SAME-ORIGIN to the Next.js route
 * handlers under `dashboard/src/app/api/**` (the catch-all `[...path]` proxy, or
 * a specific handler), which read the token server-side and inject it. Reads
 * (GET/HEAD) route through that SAME same-origin proxy (see the SSE-pooling
 * note below) -- MOST are public and carry no token, but a growing explicit
 * allowlist is token-gated too (`AUTHORIZED_READ_PREFIXES`/
 * `isAuthorizedReadPath` in `app/api/[...path]/route.ts`: accounts, system,
 * swarm, mounts, and — as of the session-ledger integration, 2026-08-04 —
 * `GET /api/ledger/claims`/`GET /api/ledger/tail`). "Reads are public" is
 * the DEFAULT, never a blanket guarantee; check that file before assuming a
 * given GET carries no token.
 *
 * Feature fetch clients build their request URL through `apiUrl(base, path,
 * method)` so this reroute is uniform: a mutation drops the FastAPI base and
 * becomes a same-origin path; a read keeps the base.
 */

/** True when `method` mutates state and must therefore carry the session token. */
export function isMutation(method?: string): boolean {
  const upper = (method ?? "GET").toUpperCase();
  return (
    upper === "POST" || upper === "PUT" || upper === "PATCH" || upper === "DELETE"
  );
}

/**
 * The URL a fetch should target. BOTH reads and mutations now go SAME-ORIGIN
 * through the Next.js proxy (`app/api/[...path]`). Mutations must (the proxy
 * injects the session token). Reads must too, because the dashboard opens ~6
 * long-lived SSE `EventSource` streams direct to FastAPI (Grok `EVENTS_BASE`
 * default `:8485`), which saturates the browser's per-host connection limit —
 * read fetches (e.g. GET /api/board) then queue behind the streams and blow
 * the 10s timeout. Routing reads through the same-origin proxy moves them off
 * the SSE host pool so they no longer compete with the event streams. `base`
 * is retained in the signature for call-site stability but is no longer used
 * for the URL.
 */
export function apiUrl(base: string, path: string, method?: string): string {
  void base;
  void method;
  return path;
}
