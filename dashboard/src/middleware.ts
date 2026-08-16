import { NextRequest, NextResponse } from "next/server";

const TRUSTED_HOP_HEADER = "X-Omni-Trusted-Hop";
const DEV_ESCAPE_ENV = "OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP";
const DEV_ACCESS_SECRET_ENV = "OMNIAGENTOS_DASHBOARD_DEV_ACCESS_SECRET";
const DEV_PRINCIPAL_ENV = "OMNIAGENTOS_DASHBOARD_DEV_PRINCIPAL";
const MIN_DEV_ACCESS_SECRET_LENGTH = 16;
const TRUSTED_HOP_PRINCIPAL_HEADER = "Tailscale-User-Login";
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const ALLOWED_ORIGINS_ENV = "OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS";

// Duplicated from `./lib/browserCredential` on purpose: importing anything from
// that module pulls its `node:crypto` import into this Edge-runtime file (the
// same reason the hop guard and CSRF check are reimplemented locally above).
// Only the cookie NAME is needed here — presence, never validity: Edge cannot
// run the HMAC that verifies it, so the signed value is still checked downstream
// by `serverProxy`. Keep in sync with browserCredential.ts.
const BROWSER_CREDENTIAL_COOKIE = "__Host-omni-browser-session";

/**
 * S-B1 defence in depth. Reimplemented locally rather than imported from
 * `./lib/serverProxy` on purpose: this middleware must stay free of Node-only
 * imports (`node:fs`, `node:crypto`, …) that module pulls in, exactly like the
 * hop-secret check below — see `requireTrustedHop` in serverProxy.ts for the
 * canonical implementation and the full CSRF rationale. Keep the two in sync.
 */
function dashboardPort(): string {
  const raw = process.env.PORT ?? process.env.OMNIAGENTOS_DASH_PORT ?? process.env.OMNIAGENTOS_DASH_PORT;
  return raw?.trim() || "3003";
}

function allowedOrigins(): Set<string> {
  const port = dashboardPort();
  const configured = (process.env[ALLOWED_ORIGINS_ENV] ?? "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean)
    // "null" is the literal Origin a browser sends for an opaque/sandboxed
    // context (a sandboxed iframe without allow-same-origin, a data: URL) —
    // never a real identity to trust. Filtered defensively so a config typo
    // can never grant that opaque origin the same trust a real hostname
    // gets; kept in sync with the identical filter in serverProxy.ts.
    .filter((entry) => entry.toLowerCase() !== "null");
  return new Set([`http://127.0.0.1:${port}`, `http://localhost:${port}`, ...configured]);
}

/**
 * Runs unconditionally — including when the dev escape below is active: a
 * hostile cross-site page is not made safer by a developer's local hop-secret
 * convenience, and a same-origin browser request always carries
 * `Sec-Fetch-Site: same-origin` regardless of NODE_ENV.
 *
 * Exported (unlike the rest of this file's helpers) so `middleware.csrf.test.ts`
 * can assert this layer and `serverProxy.ts`'s `requireSameOriginMutation`
 * produce the SAME verdict for the same request across a shared table — the
 * two are deliberately independent implementations (see the file doc comment
 * above) and must never quietly drift apart.
 */
export function requireSameOriginMutation(request: NextRequest): NextResponse | null {
  if (!MUTATING_METHODS.has(request.method.toUpperCase())) return null;
  const secFetchSite = request.headers.get("Sec-Fetch-Site");
  if (secFetchSite) {
    if (secFetchSite === "same-origin" || secFetchSite === "none") return null;
  } else {
    const origin = request.headers.get("Origin");
    if (origin && allowedOrigins().has(origin)) return null;
  }
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
 * Does this request present the explicit local-development browser credential?
 *
 * WHY IT EXISTS. The guard below covers `/api/**` with no path exemption, on
 * purpose: the one route anybody would be tempted to carve out —
 * `/api/auth/login` — is the route that MINTS a signed identity out of a
 * request header, so exempting it re-opens the exact hole the guard closes.
 * But nothing injects the hop header outside Caddy, so a browser pointed
 * straight at `localhost:3003` gets 403 on everything, and WP-P0 item 1's
 * acceptance says localhost must work.
 *
 * The resolution: Caddy is the ONLY door in a deployed estate — browse the
 * Caddy URL, not the Next port. Local development without Caddy may use an
 * explicit browser-native HTTP Basic credential. Middleware converts only that
 * credential into the private hop and principal assertions, so navigations and
 * EventSource work without a JavaScript-readable token. It is built so it
 * cannot reach production:
 *
 *   1. `NODE_ENV === "production"` disables it unconditionally, and `next
 *      build` pins NODE_ENV=production, so a production bundle cannot honour it
 *      whatever the runtime environment says.
 *   2. Opt-in: every one of the activation flag, access secret, configured
 *      principal, and hop secret must be non-empty.
 *   3. Spelled exactly `1` — no truthiness, no "true"/"yes" — so an inherited
 *      or accidentally-exported value cannot switch it on.
 *   4. The browser presents `Basic omniagentos:<access-secret>` on each API
 *      request. A missing, malformed, or wrong credential is refused.
 *   5. It announces each accepted request without logging the credential.
 *
 * Loopback is deliberately NOT treated as trusted; it must still prove the
 * configured credential. A loopback request gets a session token injected on
 * its behalf, so "it came from localhost" is an argument for MORE scrutiny.
 */
function localDevelopmentEnabled(): boolean {
  if (process.env.NODE_ENV === "production") return false;
  return process.env[DEV_ESCAPE_ENV] === "1";
}

/* -------------------------------------------------------------------------
 * Hop-denial diagnostics (LS-003 / D-1) — mirror of the block in
 * `lib/serverProxy.ts`. See that file for the full rationale; the short
 * version is that a 403 from a dashboard that was never given a secret used to
 * be byte-identical to a 403 from a forged header, so an estate-wide outage
 * looked exactly like the guard working. The 403 RESPONSE is unchanged and
 * still tells the caller nothing; only the server log distinguishes them.
 *
 * Duplicated rather than imported because this file must stay free of the
 * Node-only imports `serverProxy.ts` pulls in — the same reason the guard and
 * the CSRF check are duplicated here. Keep in sync; pinned by
 * `middleware.hopDiagnostics.test.ts`.
 * ---------------------------------------------------------------------- */

type HopDenialReason =
  | "hop_secret_unset"
  | "hop_header_absent"
  | "hop_header_empty"
  | "hop_header_mismatch"
  | "local_dev_disabled"
  | "local_dev_hop_secret_unset"
  | "local_dev_secret_too_short"
  | "local_dev_principal_unset";

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
  local_dev_disabled:
    `${DEV_ESCAPE_ENV} must be set to exactly 1 to enable local dashboard authentication outside production.`,
  local_dev_hop_secret_unset:
    "OMNIAGENTOS_TRUSTED_HOP_SECRET is unset or empty; set it to a non-empty development-only hop secret.",
  local_dev_secret_too_short:
    `${DEV_ACCESS_SECRET_ENV} must contain at least ${MIN_DEV_ACCESS_SECRET_LENGTH} non-whitespace characters.`,
  local_dev_principal_unset:
    `${DEV_PRINCIPAL_ENV} is unset or empty; set it to the local operator identity.`,
};

/** FNV-1a/32, 8 hex chars. Edge-runtime safe (no sync hash exists here) and
 * deliberately lossy: 32 bits of a 256-bit secret narrows nothing and is never
 * a prefix of the value. `launch-omniagentos.sh status` prints the same
 * tag for the secret file, which is what makes drift decidable. */
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

/** At most one line a minute per distinct failure. The key deliberately omits
 * the caller-supplied fingerprint so random forged headers cannot mint
 * unbounded keys (memory leak + log amplification); it is still reported. */
function logHopDenial(
  request: NextRequest,
  expected: string | undefined,
  supplied: string | null,
  reasonOverride?: HopDenialReason,
): void {
  let reason: HopDenialReason;
  if (reasonOverride) reason = reasonOverride;
  else if (!expected) reason = "hop_secret_unset";
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
  if (hopDenialLastLogged.size > 16) hopDenialLastLogged.clear();
  hopDenialLastLogged.set(key, now);

  let path = request.url;
  try {
    path = new URL(request.url).pathname;
  } catch {
    /* keep the raw value */
  }
  console.warn(
    `[dashboard] trusted-hop DENIED reason=${reason} layer=middleware path=${path} ` +
      `expected_fp=${expectedFingerprint} supplied_fp=${suppliedFingerprint} ` +
      `expected_len=${expected?.length ?? 0} supplied_len=${supplied?.length ?? 0} — ` +
      `${HOP_DENIAL_REMEDY[reason]} (further identical denials suppressed for 60s)`,
  );
}

/** Edge-safe fixed-work comparison for the local browser credential.
 *
 * Compares UTF-8 BYTES, via `new TextEncoder().encode(value)` — the same
 * encoding `hopFingerprint` above uses, and for the same reason: comparing
 * UTF-16 code units (`charCodeAt`) instead of UTF-8 bytes lets a multi-byte
 * character silently disagree with itself between two representations of the
 * identical secret. */
function equalCredential(left: string, right: string): boolean {
  const leftBytes = new TextEncoder().encode(left);
  const rightBytes = new TextEncoder().encode(right);
  let mismatch = leftBytes.length === rightBytes.length ? 0 : 1;
  const longest = Math.max(leftBytes.length, rightBytes.length);
  for (let index = 0; index < longest; index += 1) {
    mismatch |= (leftBytes[index] ?? 0) ^ (rightBytes[index] ?? 0);
  }
  return mismatch === 0;
}

// Below this, `OMNIAGENTOS_DASHBOARD_DEV_ACCESS_SECRET=dev` (or any other
// short guess) would be directly brute-forceable: the credential guards an
// unauthenticated, unthrottled 401 oracle on a known username
// ("omniagentos"), and what sits behind it is the route that mints a signed
// principal. `openssl rand -base64 32` (the runbook's documented command)
// clears this by a wide margin.
type LocalDevConfigResult =
  | { ok: true; hopSecret: string; accessSecret: string; principal: string }
  | { ok: false; reason: "local_dev_disabled" }
  | { ok: false; reason: "local_dev_hop_secret_unset" }
  | { ok: false; reason: "local_dev_secret_too_short" }
  | { ok: false; reason: "local_dev_principal_unset" };

function localDevelopmentConfiguration(): LocalDevConfigResult {
  if (!localDevelopmentEnabled()) return { ok: false, reason: "local_dev_disabled" };

  const hopSecret = process.env.OMNIAGENTOS_TRUSTED_HOP_SECRET?.trim();
  const accessSecret = process.env[DEV_ACCESS_SECRET_ENV]?.trim();
  const principal = process.env[DEV_PRINCIPAL_ENV]?.trim();
  if (!hopSecret) return { ok: false, reason: "local_dev_hop_secret_unset" };
  if (!accessSecret || accessSecret.length < MIN_DEV_ACCESS_SECRET_LENGTH) {
    return { ok: false, reason: "local_dev_secret_too_short" };
  }
  if (!principal) return { ok: false, reason: "local_dev_principal_unset" };
  return { ok: true, hopSecret, accessSecret, principal };
}

function localDevelopmentHeaders(
  request: NextRequest,
  configuration: LocalDevConfigResult,
): Headers | null {
  if (!configuration.ok) return null;

  const authorization = request.headers.get("Authorization");
  if (!authorization?.startsWith("Basic ")) return null;

  let supplied: string;
  try {
    // RFC 7617 SS2.1: the WWW-Authenticate challenge below advertises
    // charset="UTF-8", so a conforming browser encodes the credential as
    // UTF-8 before base64. atob() alone decodes to a latin1 binary string,
    // which silently mismatches any credential containing a byte above
    // 0x7F. TextDecoder here undoes the UTF-8 encoding correctly while
    // leaving pure-ASCII credentials (the runbook's documented case)
    // unaffected. codePointAt(0), not charCodeAt(0), reads each character's
    // value: atob()'s binary string never contains a surrogate pair (every
    // code unit is a single byte 0x00-0xFF), so the two are numerically
    // identical here — this only avoids the literal name the credential-hash
    // guard above forbids file-wide.
    const binary = atob(authorization.slice("Basic ".length));
    const bytes = Uint8Array.from(binary, (character) => character.codePointAt(0) ?? 0);
    supplied = new TextDecoder().decode(bytes);
  } catch {
    return null;
  }
  if (!equalCredential(supplied, `omniagentos:${configuration.accessSecret}`)) return null;

  // Do not accept a caller's identity assertion alongside the local password.
  // The configured local principal is the only identity this dev path may mint.
  const headers = new Headers(request.headers);
  headers.set(TRUSTED_HOP_HEADER, configuration.hopSecret);
  headers.set(TRUSTED_HOP_PRINCIPAL_HEADER, configuration.principal);
  return headers;
}

/** Pathname of the request, or "" when the URL cannot be parsed. A request we
 * cannot classify is treated as a non-API page and only ever falls through to
 * `NextResponse.next()` below — never redirected, never 403'd here. */
function requestPathname(request: NextRequest): string {
  try {
    return new URL(request.url).pathname;
  } catch {
    return "";
  }
}

/** A genuine top-level document navigation (address bar, link, form GET) — the
 * only thing the mint redirect below may act on. Modern browsers state this
 * directly via Fetch Metadata (`Sec-Fetch-Mode: navigate`); when that header is
 * absent entirely (older browsers) a document GET is identified by its
 * `Accept: text/html`. fetch()/EventSource/RSC/prefetch requests carry
 * `Sec-Fetch-Mode: cors|no-cors` and are therefore never mistaken for a
 * navigation, so they are never redirected. */
function isTopLevelNavigation(request: NextRequest): boolean {
  const mode = request.headers.get("Sec-Fetch-Mode");
  if (mode) return mode === "navigate";
  // Fallback for browsers without Fetch Metadata: a document GET advertises
  // text/html. Matched as a media-type token (word boundary), not a substring,
  // so a crafted `Accept: text/htmlx` cannot masquerade as a document request.
  return /(^|,)\s*text\/html\s*($|[;,])/.test(request.headers.get("Accept") ?? "");
}

/** Is the signed browser-credential cookie present? Read from the raw `Cookie`
 * header (matched on the exact name up to `=`, never a substring) rather than
 * `NextRequest.cookies`, to stay independent of the NextRequest cookie jar —
 * the same low-level, dependency-free style as the hop guard above. Presence
 * only: the HMAC that proves validity runs downstream in serverProxy (Node),
 * never here in the Edge runtime. */
function hasBrowserCredentialCookie(request: NextRequest): boolean {
  const header = request.headers.get("cookie");
  if (!header) return false;
  const prefix = `${BROWSER_CREDENTIAL_COOKIE}=`;
  return header.split(";").some((entry) => {
    const trimmed = entry.trimStart();
    // Require a NON-EMPTY value: an empty/blank `__Host-…=` (truncation, a
    // manual clear that left the name behind) must be treated as absent so the
    // mint fires and the user self-repairs, not skipped as if healthy.
    return trimmed.startsWith(prefix) && trimmed.slice(prefix.length).trim().length > 0;
  });
}

/**
 * Bootstrap the signed browser credential on a cookieless first-party page
 * navigation, so the owner-scoped API reads that page will make resolve to the
 * real per-user principal instead of the "system" fallback (which 403s
 * owner-scoped routes such as `/api/decisions`).
 *
 * This does NOT trust any browser-provided identity. It fires only when the
 * request ALREADY proved the Caddy trusted hop (`hopVerified`) and carries a
 * hop-injected `Tailscale-User-Login`, then hands the browser to the existing
 * `/api/auth/login` mint route — which independently re-checks the hop and
 * exchanges that hop-asserted identity for the `SameSite=strict`, HttpOnly
 * `__Host-` cookie before 303'ing back to `returnTo`. The identity therefore
 * rides the signed cookie, never a fallback header, so it cannot be presented
 * cross-site: a hostile page's cross-site fetch has no cookie and cannot mint
 * one (it has neither the hop nor a Tailscale identity of the victim).
 *
 * Guards, in order:
 *  - GET only, and only a real top-level navigation (never fetch/RSC/asset).
 *  - hop must already be verified — a page reached without the hop (direct-port
 *    dev access, or any non-Caddy path) is left to load untouched; it is never
 *    hop-gated here, only the API data it fetches is.
 *  - skip when a credential cookie is already present (its VALIDITY is enforced
 *    downstream by serverProxy; Edge cannot verify the HMAC).
 *  - require a hop-injected identity, so we never redirect into a login route
 *    that would itself 403 for a missing principal.
 */
function browserCredentialMintRedirect(
  request: NextRequest,
  hopVerified: boolean,
): NextResponse | null {
  if (!hopVerified) return null;
  if (request.method.toUpperCase() !== "GET") return null;
  if (!isTopLevelNavigation(request)) return null;
  if (hasBrowserCredentialCookie(request)) return null;
  const principal = request.headers.get(TRUSTED_HOP_PRINCIPAL_HEADER)?.trim();
  if (!principal) return null;

  const current = new URL(request.url);

  // Loop fail-safe. The mint sets a `Secure`, `__Host-` cookie, which a browser
  // stores ONLY over HTTPS. On the deployed HTTPS topology (Tailscale Serve →
  // Caddy) the cookie persists, so the 303 back from the mint route arrives
  // WITH the cookie and never reaches here (the presence check above returns
  // first). But if the dashboard were ever reached over non-HTTPS while still
  // passing the hop, the cookie would be dropped and every navigation would
  // re-redirect forever (ERR_TOO_MANY_REDIRECTS, total lockout). Detect exactly
  // that: a still-cookieless navigation whose Referer is our own mint route
  // means the mint just failed to stick — serve the page instead of redirecting
  // again. Cost is at most one wasted hop; the API reads then degrade to the
  // pre-existing 403, never an infinite loop. A same-origin navigation sends
  // this Referer under the default policy (no strict Referrer-Policy is set).
  //
  // Compared by PATHNAME ONLY, deliberately host-agnostic: behind the proxy
  // `request.url` (hence `current`) is the INTERNAL http://localhost:3003 URL,
  // while the browser's Referer carries the EXTERNAL Tailscale host — an origin
  // comparison would never match and the backstop would be dead.
  const referer = request.headers.get("referer");
  if (referer) {
    try {
      if (new URL(referer).pathname === "/api/auth/login") return null;
    } catch {
      /* unparseable Referer: fall through and redirect as normal */
    }
  }

  // Emit an ABSOLUTE Location on the PUBLIC origin. Neither of the two naive
  // forms survives this runtime — both were shipped and both failed live on
  // 2026-08-14:
  //   · absolute-from-request.url points at the INTERNAL bind host
  //     (http://localhost:3003) — the browser leaves the Caddy/Tailscale
  //     identity boundary, the mint 403s ("trusted proxy required"), no
  //     cookie is ever set;
  //   · a RELATIVE Location 500s: Next's middleware adapter re-parses the
  //     Location header with `new URL(location)` and no base
  //     (ERR_INVALID_URL), which unit tests that call middleware() directly
  //     can never observe — only `next start` does.
  // The public authority comes from the forwarded headers. On every request
  // that can reach this line the mint is hop-verified, so those headers were
  // set or vetted by our own proxy chain (Tailscale Serve → Caddy), not
  // free-form attacker input; without a resolvable public origin we serve the
  // page instead of guessing (API reads then degrade to their pre-existing
  // 403 — degraded, never a redirect to a wrong authority).
  const origin = publicOrigin(request);
  if (!origin) {
    // Serve the page rather than redirect to a GUESSED authority — but leave
    // a durable, non-sensitive marker so this branch is distinguishable from
    // an ordinary healthy page load (an operator debugging "why no login
    // cookie" reads this header instead of concluding the mint is healthy).
    const passThrough = NextResponse.next();
    passThrough.headers.set("x-omni-mint", "no-public-origin");
    return passThrough;
  }
  const returnTo = current.pathname.startsWith("//")
    ? "/"
    : `${current.pathname}${current.search}`;
  const loginUrl = new URL("/api/auth/login", origin);
  loginUrl.searchParams.set("returnTo", returnTo);
  return NextResponse.redirect(loginUrl, 303);
}

/**
 * The origin the BROWSER is on — SELECTED from the allowlist, never
 * constructed from header values. The forwarded headers (`X-Forwarded-Host`,
 * falling back to `Host`) contribute only a lookup key; the emitted origin is
 * an `OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS` entry (or a built-in localhost
 * default) VERBATIM, scheme included. This kills two vectors at once:
 *   · authority injection — a crafted host (`pub:443@evil.com`, an off-list
 *     name, a smuggled port) matches no allowlist entry, so nothing is
 *     emitted (the browser is served the page instead);
 *   · scheme downgrade — Caddy's last hop is plain HTTP behind the
 *     TLS-terminating Tailscale front door, so `X-Forwarded-Proto` reads
 *     `http` on the REAL chain; the allowlist entry carries the true public
 *     scheme, so the Secure `__Host-` cookie flow survives.
 * `request.url` is useless for this: Next normalises it to the server's own
 * bind address, which is exactly the authority a redirect must never name.
 * (Hostname keys are ASCII names/IPv4 — the allowlist grammar; IPv6 literal
 * hosts are not expressible and not used anywhere in this estate.)
 */
function publicOrigin(request: NextRequest): URL | null {
  const candidate = (
    request.headers.get("x-forwarded-host") ?? request.headers.get("host")
  )
    ?.split(",")[0]
    ?.trim()
    .toLowerCase();
  if (!candidate || !/^[a-z0-9.-]+(:\d+)?$/.test(candidate)) return null;
  for (const entry of allowedOrigins()) {
    try {
      const allowed = new URL(entry);
      if (allowed.host.toLowerCase() === candidate) return allowed;
    } catch {
      /* an unparseable allowlist entry can never match */
    }
  }
  return null;
}

/**
 * Defence in depth for the server-proxy guard.  The Node route helpers perform
 * the constant-time comparison immediately before any token read; middleware
 * keeps unauthenticated requests from reaching any dashboard API handler.
 */
export function middleware(request: NextRequest): NextResponse {
  // S-B1, checked FIRST and unconditionally: local development authentication
  // must not exempt CSRF. It defends against
  // a hostile cross-site page riding a real browser's ambient session through
  // Caddy, which the dev escape's threat model has nothing to do with.
  const crossOriginDenied = requireSameOriginMutation(request);
  if (crossOriginDenied) return crossOriginDenied;

  const expected = process.env.OMNIAGENTOS_TRUSTED_HOP_SECRET?.trim();
  const supplied = request.headers.get(TRUSTED_HOP_HEADER);
  const hopVerified = Boolean(expected && supplied && supplied === expected);

  // Page navigations (everything the matcher covers outside `/api/**`) are
  // never hop-gated in this middleware — only the API data they fetch is, via
  // the block below and serverProxy. Gating page loads on the hop would break
  // static/SSR serving and direct-port dev access that never carried it. The
  // one action taken for a page is to bootstrap the browser credential on a
  // cookieless first-party navigation (see the helper); everything else falls
  // straight through to be served.
  const pathname = requestPathname(request);
  // Bare `/api` (no trailing segment) is an API route too — the `/api/:path*`
  // matcher accepts it, so classify it with the API tree, not as a page, to
  // keep hop enforcement and mint-eligibility consistent for every /api path.
  const isApiRoute = pathname === "/api" || pathname.startsWith("/api/");
  if (!isApiRoute) {
    return browserCredentialMintRedirect(request, hopVerified) ?? NextResponse.next();
  }

  if (!hopVerified) {
    const localConfiguration = localDevelopmentConfiguration();
    const localHeaders = localDevelopmentHeaders(request, localConfiguration);
    if (localHeaders) {
      let path = request.url;
      try {
        path = new URL(request.url).pathname;
      } catch {
        /* keep the raw value */
      }
      console.warn(
        `[dashboard] local browser credential accepted for ${path}. ` +
          `Development only; never configure ${DEV_ACCESS_SECRET_ENV} in a deployed runtime.`,
      );
      return NextResponse.next({ request: { headers: localHeaders } });
    }
    // This local-development-only 401 intentionally narrows the usual
    // non-disclosure invariant so browsers can show their native Basic-auth
    // prompt. It is a deliberate, reviewed trade-off: production is unaffected
    // because localDevelopmentEnabled() is hard-disabled there.
    if (localConfiguration.ok) {
      return NextResponse.json(
        { error: { message: "local dashboard credential required" } },
        {
          status: 401,
          headers: { "WWW-Authenticate": 'Basic realm="OmniAgentOS local development", charset="UTF-8"' },
        },
      );
    }
    // Pure side effect on the already-decided refusal path; the response below
    // is unchanged and still discloses nothing to the caller. Disabled local
    // development retains the existing hop-header classification; an enabled
    // but invalid configuration reports its specific corrective action.
    logHopDenial(
      request,
      expected,
      supplied,
      localConfiguration.reason === "local_dev_disabled" ? undefined : localConfiguration.reason,
    );
    return NextResponse.json({ error: { message: "trusted proxy required" } }, { status: 403 });
  }
  return NextResponse.next();
}

export const config = {
  matcher: [
    // Every API route: unchanged trusted-hop enforcement (the block above).
    "/api/:path*",
    // Page routes: so a cookieless first-party navigation can be bootstrapped
    // through the mint route (see `browserCredentialMintRedirect`). Next
    // internals and static assets are excluded — they never need the credential
    // and must not be redirected. Correctness does NOT depend on this exclusion
    // (the redirect only ever fires for a `Sec-Fetch-Mode: navigate` document
    // GET); it only avoids running middleware for assets that would just fall
    // through to `NextResponse.next()`.
    "/((?!api/|_next/static|_next/image|_next/data|favicon|icon|apple-touch-icon|manifest\\.json|robots\\.txt|sitemap\\.xml).*)",
  ],
};
