/**
 * Server-only helpers for Next.js route handlers that proxy token-gated
 * FastAPI endpoints (SEC-005). These run in the Node.js runtime (route
 * handlers, never the browser) so the local session token is read from disk
 * server-side and injected as X-Session-Token — the browser never holds it.
 *
 * Do not import this module from any "use client" file.
 */
import { homedir } from "node:os";
import { join } from "node:path";
import { readFile } from "node:fs/promises";
import { timingSafeEqual } from "node:crypto";
import { NextResponse } from "next/server";
import {
  BROWSER_CREDENTIAL_COOKIE,
  resolveBrowserCredential,
} from "./browserCredential";
import { fetchWithTimeout, FetchTimeoutError } from "./fetchTimeout";

// Grok product defaults to :8485; set OMNIAGENTOS_API_BASE to override.
const API_BASE = process.env.OMNIAGENTOS_API_BASE ?? "http://127.0.0.1:8485";
const TRUSTED_HOP_HEADER = "X-Omni-Trusted-Hop";
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/**
 * S-B1: same-origin/CSRF enforcement for mutations. Layered on TOP of the
 * trusted-hop guard above, not a replacement for it — the hop guard proves a
 * request crossed the Caddy boundary, it does not prove WHO drove it. Caddy
 * injects the hop header (and the Tailscale identity header) on every request
 * it relays, a cross-site one included, so a hostile page loaded in the
 * operator's own browser can still ride the operator's ambient session
 * through Caddy into this proxy. This section closes that gap by checking
 * what the BROWSER itself says about the request's origin — a hostile page's
 * script cannot forge either signal (`Origin` and `Sec-Fetch-Site` are both
 * forbidden request headers).
 */

/**
 * Comma-separated, exact-match origins beyond the loopback dashboard port —
 * e.g. the Tailscale Serve hostname the operator actually browses in
 * production (`https://<host>.<tailnet>.ts.net`). Unset by default: a fresh
 * deploy trusts only loopback until an operator names the remote origin
 * explicitly here, the same fail-closed provisioning posture S-A0 uses for
 * the hop secret. No wildcarding or suffix matching — an entry must equal the
 * browser's `Origin` header byte-for-byte.
 */
const ALLOWED_ORIGINS_ENV = "OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS";

function dashboardPort(): string {
  const raw = process.env.PORT ?? process.env.OMNIAGENTOS_DASH_PORT ?? process.env.OMNIAGENTOS_DASH_PORT;
  return raw?.trim() || "3003";
}

function configuredOrigins(): string[] {
  return (process.env[ALLOWED_ORIGINS_ENV] ?? "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean)
    // "null" is the literal Origin a browser sends for an opaque/sandboxed
    // context (a sandboxed iframe without allow-same-origin, a data: URL) —
    // never a real identity to trust. Filtered defensively so an operator
    // typo/misconfiguration in this env var can never grant that opaque
    // origin the same trust a real hostname gets; Origin: null must fail
    // closed unconditionally (see the CSRF regression test for this).
    .filter((entry) => entry.toLowerCase() !== "null");
}

/** The set of Origins this dashboard instance treats as itself. */
function allowedOrigins(): Set<string> {
  const port = dashboardPort();
  return new Set([`http://127.0.0.1:${port}`, `http://localhost:${port}`, ...configuredOrigins()]);
}

/**
 * Does this request carry the browser's own proof of same-origin?
 *
 * `Sec-Fetch-Site` wins when present: `same-origin` means the initiating
 * document IS this origin; `none` means the browser generated the request
 * with no initiating document at all (typed URL, bookmark — never a mutating
 * `fetch()`). Anything else the browser reports (`cross-site`, `same-site`)
 * is a definitive refusal regardless of what `Origin` says.
 *
 * When a client sends no `Sec-Fetch-Site` at all — every non-browser client,
 * and older browsers — fall back to an exact-match `Origin` allowlist.
 * `Origin` is also a forbidden header, so this is still a browser-truthful
 * signal, just a coarser one. A request with NEITHER header present is
 * refused: see `serverProxy.csrf.test.ts` for the missing-Origin policy and
 * why it does not break any sanctioned CLI caller.
 */
function hasSameOriginSignal(request: Request): boolean {
  const secFetchSite = request.headers.get("Sec-Fetch-Site");
  if (secFetchSite) return secFetchSite === "same-origin" || secFetchSite === "none";
  const origin = request.headers.get("Origin");
  if (!origin) return false;
  return allowedOrigins().has(origin);
}

function crossOriginMutationDenied(): NextResponse {
  return NextResponse.json(
    {
      error: {
        message:
          "cross-origin mutation refused: a same-origin Sec-Fetch-Site, or an allowlisted Origin, is required",
      },
    },
    { status: 403 },
  );
}

/**
 * Refuse a mutation this browser did not originate same-origin. `null` means
 * "not denied" — either the method is not mutating (every GET/HEAD, including
 * every EventSource/SSE subscription, is untouched) or the same-origin signal
 * checked out.
 */
export function requireSameOriginMutation(request: Request): NextResponse | null {
  if (!MUTATING_METHODS.has(request.method.toUpperCase())) return null;
  if (hasSameOriginSignal(request)) return null;
  return crossOriginMutationDenied();
}

const JSON_MEDIA_TYPE = "application/json";
const MULTIPART_MEDIA_TYPE = "multipart/form-data";

/**
 * The media-type "essence" per the MIME/fetch spec: everything before the
 * first `;` (parameters — `charset=`, a multipart `boundary=`, or a bogus
 * one like `note=multipart/form-data` an attacker hopes gets substring-
 * matched), trimmed and lowercased. A prefix/substring check on the raw
 * header value is NOT safe here: `text/plain; note=multipart/form-data`
 * contains the string `multipart/form-data` without BEING it, and
 * `application/json garbage` starts with `application/json` without being
 * that type either — both must 415, and only exact essence equality gets
 * that right.
 */
function mediaTypeEssence(contentType: string): string {
  const essence = contentType.split(";", 1)[0] ?? "";
  return essence.trim().toLowerCase();
}

function unsupportedMutationContentType(): NextResponse {
  return NextResponse.json(
    {
      error: {
        message: "unsupported content type for a mutating request: JSON or multipart/form-data required",
      },
    },
    { status: 415 },
  );
}

function trustedHopSecret(): string | null {
  const secret = process.env.OMNIAGENTOS_TRUSTED_HOP_SECRET;
  return secret?.trim() || null;
}

function trustedHopDenied(): NextResponse {
  // Do not distinguish an absent local configuration from a forged header:
  // callers outside Caddy must learn neither the expected value nor whether
  // this dashboard has been activated with its hop secret yet.
  return NextResponse.json({ error: { message: "trusted proxy required" } }, { status: 403 });
}

/* -------------------------------------------------------------------------
 * Hop-denial diagnostics (LS-003 / D-1)
 *
 * The RESPONSE above stays deliberately identical for every denial — that is a
 * security property and this section does not touch it. The SERVER LOG is a
 * different audience: it is read by the operator who already owns this host.
 *
 * Before this, a 403 from a dashboard whose secret was never configured was
 * byte-identical to a 403 from a forged header, so an estate-wide outage
 * (nothing injected the header at all) looked exactly like the guard doing its
 * job. That is why LS-003 survived until a LiveSim run found it, and it is the
 * failure mode the repair plan names as this fix's own residual risk: if the
 * secret drifts between Caddy and the dashboard, everything 403s again.
 *
 * So a denial names WHICH of the four states it is:
 *   hop_secret_unset     — this process has no secret. Deployment gap; no
 *                          caller could ever succeed. NOT a rejected caller.
 *   hop_header_absent    — the request never crossed Caddy.
 *   hop_header_empty     — Caddy injected an empty value: the PROXY's copy of
 *                          the secret is unset.
 *   hop_header_mismatch  — both sides have a secret and they differ. Drift, or
 *                          a genuine forgery — the fingerprints below decide
 *                          which, and that is the distinction the plan asked
 *                          for.
 *
 * Deliberately duplicated in `middleware.ts` rather than shared, for the same
 * reason the guard itself is: middleware runs in the Edge runtime and must
 * stay free of this module's Node-only imports. Keep the two in sync.
 * ---------------------------------------------------------------------- */

type HopDenialReason =
  | "hop_secret_unset"
  | "hop_header_absent"
  | "hop_header_empty"
  | "hop_header_mismatch";

const HOP_DENIAL_REMEDY: Record<HopDenialReason, string> = {
  hop_secret_unset:
    "OMNIAGENTOS_TRUSTED_HOP_SECRET is unset in THIS dashboard process, so no caller can ever be trusted. " +
    "Start it via `scripts/launch-omniagentos.sh dashboard`, which exports it from var/secrets/trusted-hop-secret.",
  hop_header_absent:
    "the request carried no X-Omni-Trusted-Hop, so it did not come through the trusted proxy. " +
    "Browse the caddy front door (OMNIAGENTOS_CADDY_PORT, default 3013) rather than the Next port directly.",
  hop_header_empty:
    "the proxy injected an EMPTY hop header, so OMNIAGENTOS_TRUSTED_HOP_SECRET is unset in CADDY's environment. " +
    "Restart it via `scripts/launch-omniagentos.sh caddy`.",
  hop_header_mismatch:
    "a hop header arrived but did not match: the injector and this process hold DIFFERENT secrets (drift), or the header was forged. " +
    "Run `scripts/launch-omniagentos.sh status` and compare its hop fingerprint with expected_fp/supplied_fp below — " +
    "if expected_fp already equals the file's fingerprint, this was a genuinely untrusted caller, not drift.",
};

/**
 * A short, lossy, NON-reversible tag for a secret value.
 *
 * FNV-1a/32 rather than a real digest because this must produce the same tag in
 * the Edge runtime, which has no synchronous hash. That is acceptable precisely
 * because the property required is "two operators can tell whether they hold
 * the same value", not preimage resistance: it is 32 bits of a 256-bit secret,
 * so it narrows nothing, and it is NEVER a prefix or substring of the secret
 * itself. `scripts/launch-omniagentos.sh status` prints the same tag for
 * var/secrets/trusted-hop-secret, which is what makes drift decidable.
 */
function hopFingerprint(value: string): string {
  // UTF-8 BYTES, via TextEncoder — available in both the Edge runtime and Node,
  // so the two guards and the launcher's Python all hash the SAME input.
  //
  // This previously hashed `charCodeAt(index) & 0xff`: the low byte of each
  // UTF-16 code unit. For any non-ASCII secret that disagreed with the
  // launcher's UTF-8 tag ("é-secret" gave e3ca1f2b here and d184d668 there), so
  // the one feature whose job is deciding whether two sides hold the same
  // secret reported a mismatch for secrets that were identical. A diagnostic
  // that manufactures the fault it exists to detect is worse than none.
  const bytes = new TextEncoder().encode(value);
  let hash = 0x811c9dc5;
  for (let index = 0; index < bytes.length; index += 1) {
    hash ^= bytes[index];
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

const HOP_DENIAL_LOG_INTERVAL_MS = 60_000;
const hopDenialLastLogged = new Map<string, number>();

/**
 * Log a denial at most once a minute per distinct FAILURE, not per request.
 *
 * The throttle key is the reason plus the EXPECTED fingerprint — both of which
 * only an operator can change. The supplied fingerprint is reported in the
 * message but kept out of the key on purpose: keying on it would let a caller
 * sending random forged headers mint unbounded distinct keys, turning this
 * diagnostic into a memory leak and a log-amplification lever. Bounded this
 * way the map holds a handful of entries for the lifetime of the process.
 */
function logHopDenial(request: Request, expected: string | null, supplied: string | null): void {
  let reason: HopDenialReason;
  if (!expected) reason = "hop_secret_unset";
  else if (supplied === null) reason = "hop_header_absent";
  else if (supplied.trim() === "") reason = "hop_header_empty";
  else reason = "hop_header_mismatch";

  const expectedFingerprint = expected ? hopFingerprint(expected) : "unset";
  // "absent" and "empty" are distinct words on purpose: they name the two
  // different faults above, and reusing one for the other would put the
  // ambiguity back into the line whose whole job is removing it.
  const suppliedFingerprint =
    supplied === null ? "absent" : supplied.trim() === "" ? "empty" : hopFingerprint(supplied);

  const key = `${reason}:${expectedFingerprint}`;
  const now = Date.now();
  const last = hopDenialLastLogged.get(key);
  if (last !== undefined && now - last < HOP_DENIAL_LOG_INTERVAL_MS) return;
  // Backstop only: the key space is operator-sized, never caller-driven.
  if (hopDenialLastLogged.size > 16) hopDenialLastLogged.clear();
  hopDenialLastLogged.set(key, now);

  let path = request.url;
  try {
    path = new URL(request.url).pathname;
  } catch {
    /* keep the raw value */
  }
  console.warn(
    `[dashboard] trusted-hop DENIED reason=${reason} layer=serverProxy path=${path} ` +
      `expected_fp=${expectedFingerprint} supplied_fp=${suppliedFingerprint} ` +
      `expected_len=${expected?.length ?? 0} supplied_len=${supplied?.length ?? 0} — ` +
      `${HOP_DENIAL_REMEDY[reason]} (further identical denials suppressed for 60s)`,
  );
}

/**
 * Prove that this request came through the Caddy boundary before this process
 * reads or injects a bearer token.  Caddy must strip an inbound copy of this
 * header before adding its secret value; a browser-supplied header alone is
 * not proof of a trusted hop.
 */
export function requireTrustedHop(request: Request): NextResponse | null {
  const expected = trustedHopSecret();
  const supplied = request.headers.get(TRUSTED_HOP_HEADER);
  if (!expected || !supplied) {
    // Diagnostics are a pure side effect on the path that has ALREADY decided
    // to refuse. Nothing below may return, throw, or otherwise reach the
    // verdict.
    logHopDenial(request, expected, supplied);
    return trustedHopDenied();
  }

  const expectedBytes = Buffer.from(expected, "utf8");
  const suppliedBytes = Buffer.from(supplied, "utf8");
  if (suppliedBytes.length !== expectedBytes.length || !timingSafeEqual(suppliedBytes, expectedBytes)) {
    logHopDenial(request, expected, supplied);
    return trustedHopDenied();
  }
  return null;
}

function resolveProductRoot(): string {
  if (process.env.OMNIAGENTOS_HOME) return process.env.OMNIAGENTOS_HOME;
  // Prefer this monorepo when the dashboard is launched from OmniAgentOS.
  const grok = join(homedir(), "OmniAgentOS");
  const omni = join(homedir(), "OmniAgentOS");
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const { existsSync } = require("node:fs") as typeof import("node:fs");
    if (existsSync(join(grok, "var", "secrets", "sessions-token"))) return grok;
  } catch {
    /* fall through */
  }
  return omni;
}

function resolveTokenPath(): string {
  if (process.env.OMNIAGENTOS_TOKEN_PATH) return process.env.OMNIAGENTOS_TOKEN_PATH;
  return join(resolveProductRoot(), "var", "secrets", "sessions-token");
}

/** V2 dual-token gate — autonomy decisions require a distinct secret (M3). */
function resolveAutonomyTokenPath(): string {
  if (process.env.OMNIAGENTOS_AUTONOMY_TOKEN_PATH) {
    return process.env.OMNIAGENTOS_AUTONOMY_TOKEN_PATH;
  }
  return join(resolveProductRoot(), "var", "secrets", "autonomy-token");
}

/**
 * Load the server-only session secret.  Browser credentials use a
 * domain-separated HMAC derived from this value; the bearer token itself is
 * never sent to the browser.
 */
export async function readSessionToken(): Promise<string> {
  const tokenPath = resolveTokenPath();
  let raw: string;
  try {
    raw = await readFile(tokenPath, "utf8");
  } catch (reason) {
    const detail = reason instanceof Error ? reason.message : String(reason);
    throw new Error(`local session token unavailable at ${tokenPath}: ${detail}`);
  }
  const token = raw.trim();
  if (!token) throw new Error(`local session token file is empty at ${tokenPath}`);
  return token;
}

async function readAutonomyToken(): Promise<string> {
  const tokenPath = resolveAutonomyTokenPath();
  let raw: string;
  try {
    raw = await readFile(tokenPath, "utf8");
  } catch (reason) {
    const detail = reason instanceof Error ? reason.message : String(reason);
    throw new Error(`local autonomy token unavailable at ${tokenPath}: ${detail}`);
  }
  const token = raw.trim();
  if (!token) throw new Error(`local autonomy token file is empty at ${tokenPath}`);
  return token;
}

export type ProxyAuthOptions = {
  /** When true, also inject X-Autonomy-Token (human decision routes only). */
  autonomy?: boolean;
};

/**
 * Forward `request`'s body as an authorized request to `apiPath` on the FastAPI
 * server, injecting X-Session-Token read from the local token file. The
 * upstream status/body are relayed back verbatim so callers see the same
 * error shape they would from a direct FastAPI call. `method` defaults to POST;
 * pass "PUT"/"PATCH"/"DELETE" for the other mutating verbs (AC-policy fix7
 * token-gated EVERY mutating control-plane route, so the dashboard must carry
 * the token to reach any of them). `apiPath` may include a query string.
 */
export type MutatingMethod = "POST" | "PUT" | "PATCH" | "DELETE";

function relayHeaders(upstream: Response): Headers {
  const headers = new Headers();
  // ETag/Cache-Control ride along so conditional reads survive the proxy hop:
  // without the ETag reaching the browser there is nothing to send back as
  // If-None-Match, and the 304 path below can never fire.
  for (const name of [
    "Content-Type",
    "Content-Length",
    "Content-Disposition",
    "ETag",
    "Cache-Control",
    "Vary",
    // GET /api/lab/vault/search signals an incomplete result (candidate cap
    // or result-limit cut off) on this header; without it in the relay
    // allowlist a truncated search silently reads as complete in the
    // browser. See omniagentos/lab/api/routes/lab.py's vault_search.
    "X-Vault-Search-Truncated",
  ]) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  markConditional(headers, upstream.status);
  return headers;
}

/**
 * Make a conditional response uncacheable-without-revalidation, and say what it
 * varies on.
 *
 * The response to `GET /api/board` DEPENDS on the request's `If-None-Match`: the
 * same URL yields a 200 with a body or a 304 with none. Without `Vary`, an
 * intermediary is entitled to store the 304 under the URL alone and hand it to
 * the next client — one that sent no validator, has no cached copy, and is left
 * with an empty body it cannot recover from. `no-cache` does not forbid storing;
 * it forbids REUSING without revalidating, which is exactly the guarantee a
 * conditional read needs. Applied only to responses that are actually
 * conditional (they carry an ETag, or they ARE a 304), so file downloads and
 * ordinary reads keep whatever caching FastAPI asked for.
 */
function markConditional(headers: Headers, status: number): void {
  if (status !== 304 && !headers.has("ETag")) return;
  const vary = headers.get("Vary");
  const varies = vary
    ? vary
        .split(",")
        .map((part) => part.trim())
        .filter(Boolean)
    : [];
  if (!varies.some((part) => part.toLowerCase() === "if-none-match")) {
    varies.push("If-None-Match");
  }
  headers.set("Vary", varies.join(", "));
  // Never WEAKEN an upstream directive (a `no-store` must stay `no-store`);
  // only ensure revalidation is required.
  const cacheControl = headers.get("Cache-Control");
  if (!cacheControl) {
    headers.set("Cache-Control", "no-cache");
  } else if (!/\bno-cache\b|\bno-store\b/i.test(cacheControl)) {
    headers.set("Cache-Control", `no-cache, ${cacheControl}`);
  }
}

/** Pass a conditional-read header through to FastAPI (the other half of ETag). */
function conditionalHeaders(request: Request, headers: Headers): Headers {
  const ifNoneMatch = request.headers.get("If-None-Match");
  if (ifNoneMatch) headers.set("If-None-Match", ifNoneMatch);
  return headers;
}

async function relayReadResponse(upstream: Response): Promise<NextResponse> {
  const headers = relayHeaders(upstream);
  // Responses with these statuses must not include a body in the Node runtime.
  if (upstream.status === 204 || upstream.status === 304) {
    return new NextResponse(null, { status: upstream.status, headers });
  }

  // SSE MUST stream: `await upstream.arrayBuffer()` waits for the body to END,
  // and /api/events never ends — the route then never sends response headers,
  // so every EventSource sat in "Reconnecting" forever (the live-updates
  // outage on task details). Pass the body stream through untouched.
  // `no-transform` also stops the production server's compression middleware
  // from buffering the stream; Content-Length must never be set on it.
  const contentType = upstream.headers.get("Content-Type") ?? "";
  if (contentType.toLowerCase().includes("text/event-stream")) {
    return new NextResponse(upstream.body, {
      status: upstream.status,
      headers: new Headers({
        "Content-Type": contentType,
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
      }),
    });
  }

  // Do not use .text() here: project files may be images or downloads. Passing
  // the ArrayBuffer through preserves their bytes exactly and leaves FastAPI's
  // content metadata intact for the browser.
  return new NextResponse(await upstream.arrayBuffer(), { status: upstream.status, headers });
}

/** EventSource requests announce themselves via Accept; they must bypass the
 * fetch timeout's abort-on-connect budget shape (harmless there) AND carry the
 * client's abort signal so a closed/reconnected EventSource cancels its
 * upstream FastAPI stream instead of leaking one held-open connection per
 * reconnect. */
function wantsEventStream(request: Request): boolean {
  return (request.headers.get("Accept") ?? "").toLowerCase().includes("text/event-stream");
}

function upstreamFailure(reason: unknown): NextResponse {
  const detail =
    reason instanceof FetchTimeoutError
      ? reason.message
      : reason instanceof Error
        ? reason.message
        : String(reason);
  return NextResponse.json({ error: { message: `unable to reach the API: ${detail}` } }, { status: 502 });
}

function browserCredentialFromRequest(request: Request): string | undefined {
  const cookieHeader = request.headers.get("Cookie");
  if (!cookieHeader) return undefined;

  const prefix = `${BROWSER_CREDENTIAL_COOKIE}=`;
  const matches = cookieHeader
    .split(";")
    .map((part) => part.trim())
    .filter((part) => part.startsWith(prefix));
  // A host-only browser credential is unique. Treat duplicate cookie names as
  // ambiguous instead of choosing an attacker-controlled ordering rule.
  return matches.length === 1 ? matches[0]!.slice(prefix.length) : undefined;
}

/**
 * Build the complete identity-bearing upstream header set from server-owned
 * material. No inbound identity header is copied: the authenticated principal
 * exists only when the signed browser credential resolves against this exact
 * session token.
 */
function authenticatedUpstreamHeaders(request: Request, sessionToken: string): Headers {
  const headers = new Headers({ "X-Session-Token": sessionToken });
  const principal = resolveBrowserCredential(
    browserCredentialFromRequest(request),
    sessionToken,
  )?.id;
  if (principal) headers.set("X-Omni-Authenticated-Principal", principal);
  return headers;
}

export async function proxyAuthorized(
  apiPath: string,
  request: Request,
  method: MutatingMethod = "POST",
  options?: ProxyAuthOptions,
): Promise<NextResponse> {
  const denied = requireTrustedHop(request);
  if (denied) return denied;

  const crossOriginDenied = requireSameOriginMutation(request);
  if (crossOriginDenied) return crossOriginDenied;

  const contentType = request.headers.get("Content-Type") ?? "";
  const contentTypeEssence = mediaTypeEssence(contentType);
  const isMultipart = contentTypeEssence === MULTIPART_MEDIA_TYPE;
  const isJson = contentTypeEssence === JSON_MEDIA_TYPE;
  if (!isMultipart && !isJson) return unsupportedMutationContentType();

  let token: string;
  try {
    token = await readSessionToken();
  } catch (reason) {
    return NextResponse.json(
      { error: { message: reason instanceof Error ? reason.message : "session token unavailable" } },
      { status: 503 },
    );
  }

  let autonomyToken: string | null = null;
  if (options?.autonomy) {
    try {
      autonomyToken = await readAutonomyToken();
    } catch (reason) {
      return NextResponse.json(
        {
          error: {
            message: reason instanceof Error ? reason.message : "autonomy token unavailable",
          },
        },
        { status: 503 },
      );
    }
  }

  let body: BodyInit | null = "{}";
  const headers = authenticatedUpstreamHeaders(request, token);
  if (autonomyToken) headers.set("X-Autonomy-Token", autonomyToken);
  // Pass the client's actual content type through instead of relabeling it.
  // Silently rewriting every non-multipart type to application/json let a
  // "simple" (preflight-free) cross-site request send its body as e.g.
  // text/plain and have this proxy launder it into an authenticated
  // application/json call upstream — the content type is already validated
  // above (JSON or multipart only reach here), so this is never a lie.
  headers.set("Content-Type", contentType);
  try {
    if (isMultipart) {
      // Preserve the browser-provided boundary while streaming the raw form
      // bytes to FastAPI instead of buffering potentially large uploads.
      body = request.body;
    } else {
      const text = await request.text();
      if (text) body = text;
    }
  } catch {
    if (!isMultipart) body = "{}";
  }

  let upstream: Response;
  try {
    const init: RequestInit & { duplex?: "half" } = {
      method,
      headers,
      body,
      cache: "no-store",
    };
    if (isMultipart && body) init.duplex = "half";
    upstream = await fetchWithTimeout(`${API_BASE}${apiPath}`, init);
  } catch (reason) {
    // AC-reliability: a hard 10s timeout here so a wedged FastAPI backend
    // can't leave a Kill session / Approve / Reject button spinning forever
    // — the same "bounded time, clear error" guarantee as every page fetch.
    return upstreamFailure(reason);
  }

  const responseBody = await upstream.text();
  // Null-body statuses (e.g. 204 from DELETE /api/routines/{id}) must not carry a
  // body — constructing a Response with one throws in the Node runtime.
  if (upstream.status === 204 || upstream.status === 304) {
    return new NextResponse(null, { status: upstream.status });
  }
  return new NextResponse(responseBody, {
    status: upstream.status,
    headers: { "Content-Type": upstream.headers.get("Content-Type") ?? "application/json" },
  });
}

/**
 * Forward a token-gated file read to FastAPI. The token is loaded only in this
 * server-side route helper; clients receive the upstream bytes and safe file
 * response metadata, never the token itself. `apiPath` may include a query
 * string.
 */
export async function proxyRead(apiPath: string, request: Request): Promise<NextResponse> {
  const denied = requireTrustedHop(request);
  if (denied) return denied;

  let token: string;
  try {
    token = await readSessionToken();
  } catch (reason) {
    return NextResponse.json(
      { error: { message: reason instanceof Error ? reason.message : "session token unavailable" } },
      { status: 503 },
    );
  }

  let upstream: Response;
  try {
    const headers = conditionalHeaders(request, authenticatedUpstreamHeaders(request, token));
    const accept = request.headers.get("Accept");
    if (accept) headers.set("Accept", accept);
    upstream = wantsEventStream(request)
      ? await fetch(`${API_BASE}${apiPath}`, {
          method: "GET",
          headers,
          cache: "no-store",
          signal: request.signal,
        })
      : await fetchWithTimeout(`${API_BASE}${apiPath}`, {
          method: "GET",
          headers,
          cache: "no-store",
        });
  } catch (reason) {
    return upstreamFailure(reason);
  }

  return relayReadResponse(upstream);
}

/**
 * Preserve the pre-existing public-read behavior for any same-origin GET that
 * reaches the catch-all route. Unlike proxyRead(), this deliberately does not
 * load or send the session token.
 */
export async function proxyPublicRead(apiPath: string, request: Request): Promise<NextResponse> {
  let upstream: Response;
  try {
    upstream = wantsEventStream(request)
      ? await fetch(`${API_BASE}${apiPath}`, {
          method: "GET",
          headers: { Accept: "text/event-stream" },
          cache: "no-store",
          signal: request.signal,
        })
      : await fetchWithTimeout(`${API_BASE}${apiPath}`, {
          method: request.method === "HEAD" ? "HEAD" : "GET",
          headers: conditionalHeaders(request, new Headers()),
          cache: "no-store",
        });
  } catch (reason) {
    return upstreamFailure(reason);
  }

  return relayReadResponse(upstream);
}

/** Back-compat alias for the POST-only callers (decision/kill route handlers). */
export function proxyAuthorizedPost(apiPath: string, request: Request): Promise<NextResponse> {
  return proxyAuthorized(apiPath, request, "POST");
}
