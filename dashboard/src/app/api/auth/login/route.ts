import { NextRequest, NextResponse } from "next/server";
import {
  BROWSER_CREDENTIAL_COOKIE,
  BROWSER_CREDENTIAL_MAX_AGE_SECONDS,
  mintBrowserCredential,
} from "@/lib/browserCredential";
import { readSessionToken, requireTrustedHop } from "@/lib/serverProxy";

// Caddy owns this identity header today.  P0 item 2 makes Caddy strip any
// inbound copy before injecting its own; do not substitute Origin or fetch
// metadata here, because neither proves who made a navigation.
const TRUSTED_HOP_PRINCIPAL_HEADER = "Tailscale-User-Login";

/**
 * Same-origin only, decided on the RESOLVED URL rather than on the raw string.
 *
 * A prefix test (`startsWith("/") && !startsWith("//")`) is not sufficient:
 * WHATWG URL parsing treats a backslash as a slash, so `/\evil.example/phish`
 * passes both string checks and then resolves to `https://evil.example/phish`.
 * Comparing origins after parsing is the only form that cannot be talked out
 * of its own invariant.  A malformed candidate falls back to "/".
 *
 * Returns the RESOLVED URL, never a reserialised path string: the origin check
 * proves something about *this* object, and handing back
 * `pathname + search + hash` for the caller to parse a second time threw that
 * proof away.  `/..//evil.example/phish` resolves same-origin (the check
 * passes) but normalises to the pathname `//evil.example/phish`, and re-parsing
 * that as a URL reference makes `evil.example` the AUTHORITY — an off-origin
 * 303 out of the login endpoint.  Validating one representation and returning
 * another that is re-interpreted under different rules is the whole bug, so
 * there is now only one parse and the value the caller redirects to is the
 * exact value that was checked.
 */
/** Keep in sync with `publicOrigin` + `allowedOrigins` in `middleware.ts`
 * (duplicated on purpose, same reason as the hop guard: the two runtimes must
 * not share imports). Forwarded headers are a lookup KEY into the allowlist —
 * the emitted origin is an allowlist entry verbatim (scheme included), so
 * neither an injected authority nor Caddy's plain-HTTP last hop can steer the
 * redirect off the true public origin. */
function publicLoginOrigin(request: NextRequest): URL | null {
  const port =
    process.env.PORT?.trim() ||
    process.env.OMNIAGENTOS_DASH_PORT?.trim() ||
    process.env.OMNIAGENTOS_DASH_PORT?.trim() ||
    "3003";
  const configured = (process.env.OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS ?? "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean)
    .filter((entry) => entry.toLowerCase() !== "null");
  const entries = [`http://127.0.0.1:${port}`, `http://localhost:${port}`, ...configured];
  const candidate = (
    request.headers.get("x-forwarded-host") ?? request.headers.get("host")
  )
    ?.split(",")[0]
    ?.trim()
    .toLowerCase();
  if (!candidate || !/^[a-z0-9.-]+(:\d+)?$/.test(candidate)) return null;
  for (const entry of entries) {
    try {
      const allowed = new URL(entry);
      if (allowed.host.toLowerCase() === candidate) return allowed;
    } catch {
      /* an unparseable allowlist entry can never match */
    }
  }
  return null;
}

function safeReturnTo(request: NextRequest): URL {
  const base = new URL(request.url);
  const fallback = new URL("/", base);
  const candidate = request.nextUrl.searchParams.get("returnTo");
  if (!candidate || !candidate.startsWith("/")) return fallback;
  let resolved: URL;
  try {
    resolved = new URL(candidate, base);
  } catch {
    return fallback;
  }
  if (resolved.origin !== base.origin) return fallback;
  return resolved;
}

/**
 * Browser navigation entry point for the P0 credential.  A Caddy-injected
 * identity is exchanged server-side for a short-lived, signed `__Host-`
 * cookie; the FastAPI bearer token remains on disk and never enters the
 * response.  Later P0 guards resolve this cookie rather than trusting a URL
 * shape or a browser-provided identity header.
 */
export async function GET(request: NextRequest): Promise<NextResponse> {
  // FIRST, before anything is read or minted: this route signs an identity
  // asserted by a REQUEST HEADER, so without the hop boundary it is an
  // arbitrary-principal signing oracle — anyone who can reach :3003 directly
  // names themselves in a header and receives a correctly signed 8-hour
  // credential.  The same constant-time check that guards token injection
  // guards the minting of the credential that will one day replace it.
  const denied = requireTrustedHop(request);
  if (denied) return denied;

  const principal = request.headers.get(TRUSTED_HOP_PRINCIPAL_HEADER)?.trim();
  if (!principal) {
    return NextResponse.json({ error: { message: "trusted-hop identity required" } }, { status: 403 });
  }

  let sessionToken: string;
  try {
    sessionToken = await readSessionToken();
  } catch {
    // Do not disclose token-file locations from a public login endpoint.
    return NextResponse.json({ error: { message: "browser login is unavailable" } }, { status: 503 });
  }

  let credential: string;
  try {
    credential = mintBrowserCredential(principal, sessionToken);
  } catch {
    return NextResponse.json({ error: { message: "trusted-hop identity is invalid" } }, { status: 403 });
  }

  // Redirect back to `returnTo` with a RELATIVE Location. `safeReturnTo` returns
  // a URL resolved against `request.url` — which, behind Caddy/Tailscale, is the
  // INTERNAL http://localhost:3003 base. Emitting that absolute URL as Location
  // would bounce the freshly-authenticated browser to localhost, off the trusted
  // proxy, so its next API read has no hop and 403s (and the __Host- cookie just
  // set for the external host is not sent to localhost). Taking only the
  // validated path keeps the browser on the origin it is actually using.
  //
  // ONE subtlety the same-origin check does not cover for a RELATIVE reference:
  // `safeReturnTo` proves same-origin for the ABSOLUTE form, but a path that
  // normalises to a leading `//` (e.g. `/..//evil.example/phish` → pathname
  // `//evil.example/phish`) is same-origin as an absolute URL yet PROTOCOL-
  // RELATIVE as a Location — the browser would read `evil.example` as the
  // authority. Collapse any leading double-slash to a single same-origin path.
  const target = safeReturnTo(request);
  const path = `${target.pathname}${target.search}${target.hash}`;
  const safePath = path.startsWith("//") ? "/" : path;
  // Prefer an ABSOLUTE Location on the PUBLIC origin (forwarded headers — the
  // same reconstruction the middleware mint uses, duplicated per this file
  // pair's keep-in-sync idiom): the browser that just authenticated is on the
  // public host, and naming it explicitly is immune to how any runtime layer
  // re-parses the header. The relative form remains only as the no-authority
  // fallback (route responses, unlike middleware ones, are not re-parsed by
  // the Next adapter).
  const origin = publicLoginOrigin(request);
  const location = origin ? new URL(safePath, origin).toString() : safePath;
  const response = new NextResponse(null, { status: 303, headers: { Location: location } });
  if (!origin) {
    // The relative fallback is reachable only outside the proxied topology
    // (no allowlisted authority presented). Mark it so a misconfigured
    // deployment is diagnosable from the response, never mistaken for the
    // healthy absolute path.
    response.headers.set("x-omni-login-origin", "fallback-relative");
  }
  response.cookies.set({
    name: BROWSER_CREDENTIAL_COOKIE,
    value: credential,
    httpOnly: true,
    sameSite: "strict",
    secure: true,
    path: "/",
    maxAge: BROWSER_CREDENTIAL_MAX_AGE_SECONDS,
  });
  response.headers.set("Cache-Control", "no-store");
  return response;
}
