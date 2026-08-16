import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { BROWSER_CREDENTIAL_COOKIE, resolveBrowserCredential } from "@/lib/browserCredential";

const { readSessionTokenMock, requireTrustedHopMock } = vi.hoisted(() => ({
  readSessionTokenMock: vi.fn(),
  requireTrustedHopMock: vi.fn(),
}));

vi.mock("@/lib/serverProxy", () => ({
  readSessionToken: readSessionTokenMock,
  requireTrustedHop: requireTrustedHopMock,
}));

import { GET } from "./route";

function nextRequest(url: string, headers?: HeadersInit): NextRequest {
  const parsed = new URL(url);
  // Host modelled by default (every real browser sends it); the public-origin
  // reconstruction reads it. Hostless tests pass `Host: ""`.
  const request = new Request(url, {
    headers: { Host: parsed.host, ...Object.fromEntries(new Headers(headers ?? {}).entries()) },
  }) as NextRequest;
  Object.defineProperty(request, "nextUrl", { value: parsed, configurable: true });
  return request;
}

describe("browser credential login route", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });
  beforeEach(() => {
    vi.stubEnv(
      "OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS",
      "https://dashboard.example.test,https://front.example.test",
    );
    readSessionTokenMock.mockReset();
    readSessionTokenMock.mockResolvedValue("server-only-session-token");
    // Default: the request arrived through the trusted hop. The refusal case
    // below flips this, because a route that mints signed identities must be
    // proven to STOP when the boundary says no.
    requireTrustedHopMock.mockReset();
    requireTrustedHopMock.mockReturnValue(null);
  });

  it("exchanges the trusted-hop identity for a secure host-only browser credential", async () => {
    const response = await GET(
      nextRequest("https://dashboard.example.test/api/auth/login?returnTo=%2Fapprovals", {
        "Tailscale-User-Login": "owner@example.test",
      }),
    );

    expect(response.status).toBe(303);
    // ABSOLUTE on the public (forwarded/Host) origin — immune to how any
    // runtime layer re-parses the header, and never the internal bind host.
    expect(response.headers.get("location")).toBe("https://dashboard.example.test/approvals");
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    const setCookie = response.headers.get("set-cookie")!;
    expect(setCookie).toContain(`${BROWSER_CREDENTIAL_COOKIE}=`);
    expect(setCookie).toContain("Path=/");
    expect(setCookie).toContain("HttpOnly");
    expect(setCookie).toContain("Secure");
    expect(setCookie).toMatch(/SameSite=strict/i);
    expect(setCookie).not.toContain("Domain=");

    const value = setCookie.match(new RegExp(`${BROWSER_CREDENTIAL_COOKIE}=([^;]+)`))?.[1];
    expect(resolveBrowserCredential(value, "server-only-session-token")).toEqual({ id: "owner@example.test" });
  });

  it("names the PUBLIC forwarded origin even when request.url is the internal localhost (the proxy case)", async () => {
    // The regression this guards: behind Caddy/Tailscale, request.url is
    // http://localhost:3003/... . An absolute redirect built from it bounces the
    // just-authenticated browser to localhost — off the trusted proxy — where its
    // next API read has no hop and 403s. The Location must name the PUBLIC
    // origin the browser is actually on, reconstructed from the forwarded
    // headers (a bare relative Location is not an option estate-wide: Next's
    // middleware adapter 500s on it, so both emitters use one absolute model).
    const response = await GET(
      nextRequest("http://localhost:3003/api/auth/login?returnTo=%2Fapprovals", {
        "Tailscale-User-Login": "owner@example.test",
        "X-Forwarded-Host": "dashboard.example.test",
        "X-Forwarded-Proto": "https",
      }),
    );

    expect(response.status).toBe(303);
    const location = response.headers.get("location")!;
    expect(location).toBe("https://dashboard.example.test/approvals");
    expect(location).not.toContain("localhost");
  });

  it("refuses to mint a credential without a trusted-hop identity", async () => {
    const response = await GET(nextRequest("https://dashboard.example.test/api/auth/login"));

    expect(response.status).toBe(403);
    expect(readSessionTokenMock).not.toHaveBeenCalled();
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("refuses a trusted-hop principal containing control characters", async () => {
    const response = await GET(
      nextRequest("https://dashboard.example.test/api/auth/login", {
        "Tailscale-User-Login": "owner\u0001@example.test",
      }),
    );

    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ error: { message: "trusted-hop identity is invalid" } });
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("never redirects a successful login to an external URL", async () => {
    const response = await GET(
      nextRequest("https://dashboard.example.test/api/auth/login?returnTo=https%3A%2F%2Fevil.example", {
        "Tailscale-User-Login": "owner@example.test",
      }),
    );

    expect(response.headers.get("location")).toBe("https://dashboard.example.test/");
  });

  it("refuses to mint anything when the trusted-hop boundary denies the request", async () => {
    // Without this, the route is an arbitrary-principal signing oracle: any
    // caller reaching :3003 directly names themselves in a header and gets a
    // correctly signed 8-hour credential for that identity.
    requireTrustedHopMock.mockReturnValue(
      NextResponse.json({ error: { message: "forbidden" } }, { status: 403 }),
    );

    const response = await GET(
      nextRequest("https://dashboard.example.test/api/auth/login", {
        "Tailscale-User-Login": "attacker@evil.example",
        "X-Omni-Trusted-Hop": "forged",
      }),
    );

    expect(response.status).toBe(403);
    expect(readSessionTokenMock).not.toHaveBeenCalled();
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("never redirects to an off-origin URL smuggled through a backslash", async () => {
    // WHATWG URL parsing treats `\` as `/`, so `/\evil.example/phish` passes a
    // startsWith("/") && !startsWith("//") prefix test and then resolves to
    // https://evil.example/phish. The check must compare RESOLVED origins.
    const response = await GET(
      nextRequest("https://dashboard.example.test/api/auth/login?returnTo=%2F%5Cevil.example%2Fphish", {
        "Tailscale-User-Login": "owner@example.test",
      }),
    );

    expect(response.headers.get("location")).toBe("https://dashboard.example.test/");
  });

  it("never redirects off-origin when the validated candidate normalises to a protocol-relative path", async () => {
    // The backslash and leading-`//` forms above are both caught, because the
    // CANDIDATE resolves off-origin. `/..//evil.example/` does not: it resolves
    // to https://dashboard.example.test//evil.example/ — same origin, so the
    // check passes — and WHATWG path normalisation leaves `//evil.example/` in
    // `resolved.pathname`. The route then re-parses that pathname against
    // request.url to build the Location, and a protocol-relative reference
    // resolves to the AUTHORITY `evil.example`. Validating a candidate and
    // returning a value that is re-parsed under different rules is the bug:
    // safeReturnTo must return a reference that can only ever be same-origin.
    const response = await GET(
      nextRequest("https://dashboard.example.test/api/auth/login?returnTo=%2F..%2F%2Fevil.example%2Fphish", {
        "Tailscale-User-Login": "owner@example.test",
      }),
    );

    // With a relative Location, a pathname of `//evil.example/phish` would be
    // PROTOCOL-RELATIVE (browser reads `evil.example` as the authority). The
    // route's leading-`//` collapse neutralises it to the same-origin root "/".
    const location = response.headers.get("location")!;
    expect(location).toBe("https://dashboard.example.test/");
    expect(location.startsWith("//")).toBe(false);
  });

  it("neutralises triple-slash and encoded-double-slash returnTo variants to a same-origin relative path", async () => {
    // Regression pins for the two protocol-relative edge cases: `///evil` (which
    // normalises to a pathname beginning `//`) collapses to "/", and a
    // %2F%2F-encoded returnTo stays a LITERAL same-origin path (never triggers
    // protocol-relative browser behaviour). Neither may ever yield a Location
    // that starts with "//" or names an external authority.
    for (const returnTo of ["%2F%2F%2Fevil.example%2Fphish", "%2F%252F%252Fevil.example", "%2F%2Fevil.example"]) {
      const response = await GET(
        nextRequest(`https://dashboard.example.test/api/auth/login?returnTo=${returnTo}`, {
          "Tailscale-User-Login": "owner@example.test",
        }),
      );
      const location = response.headers.get("location")!;
      const resolved = new URL(location);
      expect(resolved.origin).toBe("https://dashboard.example.test");
      expect(resolved.pathname.startsWith("//")).toBe(false);
      expect(location.toLowerCase()).not.toContain("evil.example/phish");
    }
  });

  it("falls back to a RELATIVE Location with a diagnostic marker when no allowlisted authority is presented", async () => {
    const response = await GET(
      nextRequest("http://localhost:9999/api/auth/login?returnTo=%2Fapprovals", {
        "Tailscale-User-Login": "owner@example.test",
        Host: "unlisted.example",
      }),
    );
    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("/approvals");
    expect(response.headers.get("x-omni-login-origin")).toBe("fallback-relative");
  });

  it("does not reveal server token paths when the signing secret is unavailable", async () => {
    readSessionTokenMock.mockRejectedValue(new Error("secret path: /private/token"));

    const response = await GET(
      nextRequest("https://dashboard.example.test/api/auth/login", {
        "Tailscale-User-Login": "owner@example.test",
      }),
    );

    expect(response.status).toBe(503);
    expect(await response.text()).not.toContain("/private/token");
  });
});
