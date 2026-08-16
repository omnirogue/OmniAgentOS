import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { NextRequest } from "next/server";
import { config, middleware } from "./middleware";

function request(headers?: HeadersInit, init: RequestInit = {}): NextRequest {
  return new Request("https://dashboard.example.test/api/access/servers", { ...init, headers }) as NextRequest;
}

function localDevAuthorization(secret = "local-browser-secret"): string {
  return `Basic ${btoa(`omniagentos:${secret}`)}`;
}

/** A page (non-/api) request at `path`, defaulting to a genuine top-level
 * document navigation (`Sec-Fetch-Mode: navigate`). Individual tests override
 * headers to model fetch/RSC/asset requests or an already-authenticated load. */
function pageRequest(path: string, headers: Record<string, string> = {}): NextRequest {
  return new Request(`https://dashboard.example.test${path}`, {
    // Host is modelled by default because every real browser sends it — the
    // public-origin reconstruction (and the loop backstop) read it. Tests that
    // model a hostless client override it with an empty string.
    headers: { "Sec-Fetch-Mode": "navigate", Host: "dashboard.example.test", ...headers },
  }) as NextRequest;
}

const HOP = "caddy-injected-secret";

describe("dashboard API trusted-hop middleware", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("matches every API route and fails closed when the Caddy secret is absent", async () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", "caddy-injected-secret");

    // The matcher now also covers page routes (to bootstrap the browser
    // credential on a cookieless navigation), but the /api tree — and its
    // fail-closed hop enforcement — is unchanged.
    expect(config.matcher).toContain("/api/:path*");
    expect(middleware(request()).status).toBe(403);
    expect(middleware(request({ "X-Omni-Trusted-Hop": "forged" })).status).toBe(403);
  });

  it("permits the exact Caddy-injected secret", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", "caddy-injected-secret");

    expect(middleware(request({ "X-Omni-Trusted-Hop": "caddy-injected-secret" })).status).toBe(200);
  });

  it("permits a legitimate SAME-ORIGIN mutation (positive CSRF path, not just the reject path)", () => {
    // Guards against a regression that blocks ALL mutations: the CSRF layer must
    // let a same-origin, hop-verified POST through, not only refuse cross-site.
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", "caddy-injected-secret");

    const response = middleware(
      request(
        { "X-Omni-Trusted-Hop": "caddy-injected-secret", "Sec-Fetch-Site": "same-origin" },
        { method: "POST", body: "{}" },
      ),
    );
    expect(response.status).toBe(200);
  });

  // --- local development (WP-P0 item 1: localhost must work) ----------------
  // A browser cannot present a custom hop header on navigation or EventSource,
  // so localhost uses HTTP Basic authentication.  The middleware turns a valid
  // local credential into the internal assertion; it is never a no-hop bypass.

  it("PRODUCTION IGNORES local development authentication, even when it is configured", () => {
    // The one that matters. `next build` pins NODE_ENV=production, so a
    // production bundle cannot honour this however the runtime is configured.
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", "caddy-injected-secret");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP", "1");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ACCESS_SECRET", "local-browser-secret");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_PRINCIPAL", "operator@example.test");

    expect(middleware(request({ Authorization: localDevAuthorization() })).status).toBe(403);
    expect(middleware(request({ "X-Omni-Trusted-Hop": "forged", Authorization: localDevAuthorization() })).status).toBe(403);
  });

  it("turns a configured browser-native local credential into internal assertions", () => {
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", "caddy-injected-secret");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP", "1");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ACCESS_SECRET", "local-browser-secret");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_PRINCIPAL", "operator@example.test");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    const response = middleware(
      request({
        Authorization: localDevAuthorization(),
        "X-Omni-Trusted-Hop": "forged",
        "Tailscale-User-Login": "forged@example.test",
      }),
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("x-middleware-request-x-omni-trusted-hop")).toBe("caddy-injected-secret");
    expect(response.headers.get("x-middleware-request-tailscale-user-login")).toBe("operator@example.test");
    expect(warn).toHaveBeenCalledOnce();
    expect(warn.mock.calls[0][0]).toContain("local browser credential accepted");
    expect(warn.mock.calls[0][0]).toContain("/api/access/servers");
    warn.mockRestore();
  });

  it("fails closed unless every exact local-development setting and credential is present", () => {
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", "caddy-injected-secret");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ACCESS_SECRET", "local-browser-secret");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_PRINCIPAL", "operator@example.test");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    for (const value of [undefined, "", "true", "TRUE", "yes", "0"]) {
      vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP", value as string);
      expect(middleware(request({ Authorization: localDevAuthorization() })).status).toBe(403);
    }

    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP", "1");
    const absentCredential = middleware(request());
    expect(absentCredential.status).toBe(401);
    expect(absentCredential.headers.get("WWW-Authenticate")).toContain("Basic");
    expect(middleware(request({ Authorization: localDevAuthorization("wrong") })).status).toBe(401);
    warn.mockClear();
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_PRINCIPAL", "");
    expect(middleware(request({ Authorization: localDevAuthorization() })).status).toBe(403);
    expect(warn).toHaveBeenCalled();
    const line = String(warn.mock.calls.at(-1)?.[0] ?? "");
    expect(line).toContain("reason=local_dev_principal_unset");
    expect(line).not.toContain("reason=hop_header");
  });

  it("refuses cross-site mutations before accepting a valid local credential", async () => {
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", "caddy-injected-secret");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP", "1");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ACCESS_SECRET", "local-browser-secret");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_PRINCIPAL", "operator@example.test");

    const response = middleware(
      request(
        {
          Authorization: localDevAuthorization(),
          Origin: "https://hostile.example.test",
          "Sec-Fetch-Site": "cross-site",
        },
        { method: "POST", body: "{}" },
      ),
    );

    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({
      error: {
        message:
          "cross-origin mutation refused: a same-origin Sec-Fetch-Site, or an allowlisted Origin, is required",
      },
    });
    expect(response.headers.get("x-middleware-request-x-omni-trusted-hop")).toBeNull();
  });

  it("does not treat loopback as trusted", () => {
    // The assumption that created the :3003 finding: a loopback request to this
    // dashboard gets a session token injected on its behalf.
    vi.stubEnv("NODE_ENV", "development");
    const loopback = new Request("http://localhost:3003/api/access/servers") as NextRequest;

    expect(middleware(loopback).status).toBe(403);
  });

  it("refuses a local access secret shorter than 16 characters, even if it matches exactly", () => {
    // A short secret behind an unauthenticated, unthrottled 401 oracle on a
    // known username ("omniagentos") is directly brute-forceable. The runbook
    // documents `openssl rand -base64 32`; this enforces a floor instead of
    // relying on prose alone.
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", "caddy-injected-secret");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP", "1");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ACCESS_SECRET", "short-15chars!!"); // 15 chars
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_PRINCIPAL", "operator@example.test");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    const response = middleware(request({ Authorization: localDevAuthorization("short-15chars!!") }));

    expect(response.status).toBe(403);
    expect(warn).toHaveBeenCalled();
    const line = String(warn.mock.calls.at(-1)?.[0] ?? "");
    expect(line).toContain("reason=local_dev_secret_too_short");
    expect(line).not.toContain("reason=hop_header");
  });

  it("trims the configured local access secret before validating and comparing it", () => {
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", "caddy-injected-secret");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP", "1");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ACCESS_SECRET", "  local-browser-secret  ");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_PRINCIPAL", "operator@example.test");
    vi.spyOn(console, "warn").mockImplementation(() => {});

    expect(middleware(request({ Authorization: localDevAuthorization() })).status).toBe(200);

    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ACCESS_SECRET", " ".repeat(16));
    expect(middleware(request({ Authorization: localDevAuthorization(" ".repeat(16)) })).status).toBe(403);
  });

  it("accepts a non-ASCII local access secret encoded as UTF-8, per the advertised charset", () => {
    // WWW-Authenticate advertises charset="UTF-8" (RFC 7617 SS2.1), so a
    // conforming browser encodes the credential as UTF-8 before base64.
    // atob() alone decodes to latin1 and silently mismatches any byte above
    // 0x7F; TextDecoder must be used to honour the advertised charset.
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", "caddy-injected-secret");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP", "1");
    const nonAsciiSecret = "café-münchen-ß-secret16";
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ACCESS_SECRET", nonAsciiSecret);
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_PRINCIPAL", "operator@example.test");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    const utf8Bytes = new TextEncoder().encode(`omniagentos:${nonAsciiSecret}`);
    const binaryString = Array.from(utf8Bytes, (byte) => String.fromCharCode(byte)).join("");
    const authorization = `Basic ${btoa(binaryString)}`;

    const response = middleware(request({ Authorization: authorization }));

    expect(response.status).toBe(200);
    expect(response.headers.get("x-middleware-request-x-omni-trusted-hop")).toBe("caddy-injected-secret");
    warn.mockRestore();
  });
});

// The security-critical property of the browser-credential bootstrap: it mints
// the SameSite=strict cookie ONLY from a hop-verified, Tailscale-identified,
// cookieless top-level navigation, and NEVER on any other request shape — so the
// identity always rides the signed cookie, never a header a cross-site caller
// could present.
describe("dashboard browser-credential mint redirect", () => {
  beforeEach(() => {
    // The emitters SELECT from this allowlist (scheme carried by the entry);
    // header values are only lookup keys. Mirrors production, where the
    // launchd env pins the Tailscale front-door origin.
    vi.stubEnv(
      "OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS",
      "https://dashboard.example.test,https://front.example.test",
    );
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  const NAV = { "X-Omni-Trusted-Hop": HOP, "Tailscale-User-Login": "owner@acmeuni.example" };

  it("redirects a cookieless hop-verified navigation through the mint route, preserving the destination", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    const response = middleware(pageRequest("/decisions?tab=alerts", NAV));

    expect(response.status).toBe(303);
    const raw = response.headers.get("location") ?? "";
    // ABSOLUTE on the PUBLIC origin (from the forwarded/Host headers), never on
    // the internal bind host and never relative: request.url-based absolutes
    // stranded browsers on localhost:3003, and relative Locations 500 at
    // runtime (Next's middleware adapter re-parses Location with no base) —
    // both shipped and both failed live on 2026-08-14.
    expect(raw).not.toContain("localhost");
    const location = new URL(raw);
    expect(location.origin).toBe("https://dashboard.example.test");
    expect(location.pathname).toBe("/api/auth/login");
    expect(location.searchParams.get("returnTo")).toBe("/decisions?tab=alerts");
  });

  it("prefers X-Forwarded-Host over Host for the redirect authority", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    const response = middleware(
      pageRequest("/decisions", { ...NAV, "X-Forwarded-Host": "front.example.test", "X-Forwarded-Proto": "https" }),
    );
    expect(response.status).toBe(303);
    expect(new URL(response.headers.get("location") ?? "").origin).toBe("https://front.example.test");
  });

  it("never redirects to an authority outside the allowlist (injection-shaped or plain off-list)", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    for (const hostile of [
      "evil.example",
      "dashboard.example.test:443@evil.example",
      "dashboard.example.test/..",
      "dashboard.example.test:9999",
    ]) {
      const response = middleware(pageRequest("/decisions", { ...NAV, "X-Forwarded-Host": hostile }));
      expect(response.status, hostile).toBe(200);
      expect(response.headers.get("location"), hostile).toBeNull();
      expect(response.headers.get("x-omni-mint"), hostile).toBe("no-public-origin");
    }
  });

  it("emits the allowlist entry's SCHEME even when the last proxy hop reports plain http", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    // The real chain: TLS terminates at Tailscale Serve; Caddy's inbound leg
    // is plain HTTP, so X-Forwarded-Proto reads http. The redirect must stay
    // on the allowlisted https origin or the Secure __Host- cookie dies.
    const response = middleware(
      pageRequest("/decisions", { ...NAV, "X-Forwarded-Proto": "http" }),
    );
    expect(response.status).toBe(303);
    expect(new URL(response.headers.get("location") ?? "").origin).toBe(
      "https://dashboard.example.test",
    );
  });

  it("serves the page instead of guessing when no public authority is resolvable", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    // No Host, no X-Forwarded-Host: a redirect would have to invent an
    // authority. Serving the page degrades API reads to their existing 403 —
    // never a redirect to a wrong (internal or guessed) origin.
    const response = middleware(pageRequest("/decisions", { ...NAV, Host: "" }));
    expect(response.status).toBe(200);
    expect(response.headers.get("location")).toBeNull();
  });

  it("does not redirect once the browser credential cookie is present (validity is enforced downstream)", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    const response = middleware(
      pageRequest("/decisions", { ...NAV, Cookie: "__Host-omni-browser-session=header.payload.sig" }),
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("location")).toBeNull();
  });

  it("does not redirect a non-navigation page request (fetch/RSC/EventSource), even without a cookie", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    for (const mode of ["cors", "no-cors", "same-origin", "websocket"]) {
      const response = middleware(pageRequest("/decisions", { ...NAV, "Sec-Fetch-Mode": mode }));
      expect(response.status).toBe(200);
      expect(response.headers.get("location")).toBeNull();
    }
  });

  it("does not redirect a navigation that never proved the trusted hop", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    // No hop header at all, and a forged one: a page reached without Caddy is
    // served, not redirected into a mint route it could never satisfy.
    expect(middleware(pageRequest("/decisions", { "Tailscale-User-Login": "owner@x" })).status).toBe(200);
    const forged = middleware(
      pageRequest("/decisions", { "X-Omni-Trusted-Hop": "forged", "Tailscale-User-Login": "owner@x" }),
    );
    expect(forged.status).toBe(200);
    expect(forged.headers.get("location")).toBeNull();
  });

  it("does not redirect when the hop is verified but no Tailscale identity was injected", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    // Guards against redirecting into a login route that would itself 403.
    const response = middleware(pageRequest("/decisions", { "X-Omni-Trusted-Hop": HOP }));
    expect(response.status).toBe(200);
    expect(response.headers.get("location")).toBeNull();
  });

  it("does not redirect a non-GET navigation", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    const response = middleware(
      new Request("https://dashboard.example.test/decisions", {
        method: "POST",
        headers: { ...NAV, "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "same-origin" },
        body: "{}",
      }) as NextRequest,
    );
    // Same-origin POST passes the CSRF guard, then falls through as a page — no mint redirect.
    expect(response.headers.get("location")).toBeNull();
  });

  it("never mint-redirects an /api route even under full navigation conditions: the /api guard owns those", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    // Every mint precondition is met (hop + identity + Sec-Fetch-Mode: navigate
    // + no cookie) so the ONLY thing that can prevent a 303 is the `/api` branch
    // itself — this exercises the guard, unlike a plain fetch which would fail
    // isTopLevelNavigation anyway. Covers both `/api/...` and the bare `/api`.
    for (const path of ["/api/decisions", "/api"]) {
      const response = middleware(pageRequest(path, NAV)); // pageRequest sets Sec-Fetch-Mode: navigate
      expect(response.status).toBe(200);
      expect(response.headers.get("location")).toBeNull();
    }
  });

  it("identifies a document navigation by Accept when Fetch Metadata is absent (older browsers)", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    const request = new Request("https://dashboard.example.test/decisions", {
      headers: { Host: "dashboard.example.test", "X-Omni-Trusted-Hop": HOP, "Tailscale-User-Login": "owner@x", Accept: "text/html,application/xhtml+xml" },
    }) as NextRequest;
    expect(middleware(request).status).toBe(303);

    const nonDocument = new Request("https://dashboard.example.test/decisions", {
      headers: { Host: "dashboard.example.test", "X-Omni-Trusted-Hop": HOP, "Tailscale-User-Login": "owner@x", Accept: "application/json" },
    }) as NextRequest;
    expect(middleware(nonDocument).status).toBe(200);
    expect(nonDocument && middleware(nonDocument).headers.get("location")).toBeNull();
  });

  it("does NOT re-redirect a cookieless navigation that just came from the mint route (loop fail-safe)", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    // Models a non-HTTPS topology where the Secure __Host- cookie failed to
    // persist: the 303 back from /api/auth/login lands here still cookieless.
    // Redirecting again would loop forever; the Referer backstop serves instead.
    const response = middleware(
      pageRequest("/decisions", { ...NAV, Referer: "https://dashboard.example.test/api/auth/login?returnTo=%2Fdecisions" }),
    );
    expect(response.status).toBe(200);
    expect(response.headers.get("location")).toBeNull();
  });

  it("STILL redirects a normal inbound navigation whose Referer is a different path", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    // The backstop keys on the Referer PATHNAME only (host-agnostic — see the
    // helper: `current` is the internal localhost URL, so it cannot know its own
    // external host). A Referer whose path is NOT /api/auth/login must still get
    // the mint.
    expect(
      middleware(pageRequest("/decisions", { ...NAV, Referer: "https://dashboard.example.test/approvals" })).status,
    ).toBe(303);
  });

  it("fires the loop backstop for any host's /api/auth/login Referer (host-agnostic by necessity)", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    // Because the backstop cannot compare against its own external origin, a
    // Referer path of /api/auth/login from ANY host serves instead of redirecting.
    // This is a deliberate, safe trade: the worst case is one navigation not
    // auto-logging-in (a reload fixes it), never a loop or an auth bypass — the
    // served page's API reads still enforce identity downstream.
    const response = middleware(
      pageRequest("/decisions", { ...NAV, Referer: "https://evil.example/api/auth/login" }),
    );
    expect(response.status).toBe(200);
    expect(response.headers.get("location")).toBeNull();
  });

  it("never promotes Tailscale-User-Login into a trusted identity header (the #439 invariant, pinned)", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    // The mint path must hand identity to the browser ONLY as the signed cookie
    // (via the login route), never by elevating the raw Caddy header into a
    // principal assertion the app would trust. Assert middleware forwards the
    // request untouched: no injected principal/identity override header. A
    // regression that re-copied the header here would reopen #439 with CI green.
    const forwarded = middleware(
      new Request("https://dashboard.example.test/api/decisions", {
        headers: { "X-Omni-Trusted-Hop": HOP, "Tailscale-User-Login": "owner@acmeuni.example" },
      }) as NextRequest,
    );
    expect(forwarded.headers.get("x-middleware-request-x-omni-authenticated-principal")).toBeNull();
    expect(forwarded.headers.get("x-middleware-request-tailscale-user-login")).toBeNull();
  });

  it("treats an empty __Host- cookie value as absent and mints (finding-3: no favourable-absence)", () => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP);

    // A blank/truncated credential value must not read as a healthy session and
    // freeze the user; it should re-mint.
    const response = middleware(
      pageRequest("/decisions", { ...NAV, Cookie: "__Host-omni-browser-session=" }),
    );
    expect(response.status).toBe(303);
    expect(new URL(response.headers.get("location") ?? "", "https://dashboard.example.test").pathname).toBe(
      "/api/auth/login",
    );
  });
});
