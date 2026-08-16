import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * S-B1 — same-origin/CSRF enforcement on dashboard mutations.
 *
 * THE GAP THIS CLOSES: `requireTrustedHop` (serverProxy.trustedHop.test.ts)
 * proves a request crossed the Caddy boundary. It does NOT prove who drove
 * it — Caddy injects the hop header (and the Tailscale identity header) on
 * every request it relays, a cross-site one included. A hostile page loaded
 * in the operator's own browser tab can still issue a same-origin-looking
 * mutation through Caddy, riding the operator's ambient session, and the hop
 * header alone would wave it through. This file proves the independent
 * second layer that closes that gap, plus the companion content-type-
 * laundering fix (a "simple", preflight-free cross-site request could smuggle
 * a JSON-shaped body in under e.g. `text/plain`, and the proxy used to
 * silently relabel it `application/json` before forwarding upstream).
 *
 * === MISSING-ORIGIN-HEADER POLICY: fail closed, decided explicitly ===
 *
 * A mutation with NEITHER a same-origin/none `Sec-Fetch-Site` verdict NOR an
 * allowlisted `Origin` is REFUSED (403) — see
 * "the missing-Origin policy: fail closed" below.
 *
 * Rationale, cross-checked against `scripts/gates/identity-topology-probe.sh`
 * (the repo's own record of how CLI/curl operators authenticate against this
 * stack): that probe's four credential classes (`no-creds`, `forged-owner`,
 * `forged-non-owner`, `machine-token`) issue raw curl mutations with NEITHER an
 * `Origin` NOR a `Sec-Fetch-Site` header — curl never sends either header
 * unless a caller explicitly adds it. None of those four represents a
 * sanctioned caller that would be newly broken by failing closed here:
 *
 *   - `no-creds` / `forged-owner` / `forged-non-owner` carry no valid credential
 *     of any kind against this dashboard's own :3003 proxy — they are
 *     probing the identity boundary itself, not performing a legitimate
 *     mutation, and every one of them is denied 403 regardless. ORDERING
 *     NOTE (corrected post-review): at the real HTTP entry point,
 *     `middleware.ts` runs before any route handler, and it now checks
 *     same-origin BEFORE the trusted-hop secret — unconditionally, so the
 *     local dev escape can't accidentally exempt CSRF too (see
 *     middleware.ts's comment on `requireSameOriginMutation`). So a bare
 *     curl mutation with neither `Origin` nor `Sec-Fetch-Site` now fails at
 *     THIS check, at the middleware layer, not at `requireTrustedHop` — the
 *     status code (403) and the practical outcome (never reaches a route
 *     handler) are unchanged, only which guard is the one that actually
 *     fires first. (Inside `proxyAuthorized` itself — reachable only if
 *     middleware already let the request through — `requireTrustedHop` still
 *     runs before this same-origin check; that inner ordering is unchanged
 *     and is what `serverProxy.trustedHop.test.ts` pins.)
 *   - `machine-token` (a real, valid `X-Session-Token`) is the genuine
 *     sanctioned-CLI credential in that probe, but it authenticates directly
 *     against FastAPI on :8485 (outside this file's ownership; see S-B2).
 *     THIS dashboard proxy never reads a client-supplied `X-Session-Token` —
 *     it always injects its OWN disk-resident token
 *     (`readSessionToken()`) — so a script holding the machine token has no
 *     reason to route a mutation through :3003 at all, and doing so already
 *     gets the caller nothing a direct FastAPI call would not.
 *
 * In short: there is no sanctioned CLI path that mutates THROUGH the
 * dashboard's own proxy today. The only real caller of `proxyAuthorized` is
 * the dashboard's own browser JavaScript (see `lib/api.ts`, which always sets
 * an explicit `Content-Type`), and every same-origin browser fetch of any
 * age reliably sends `Sec-Fetch-Site: same-origin` — so failing closed here
 * costs nothing sanctioned and closes a real gap.
 */

const { fetchWithTimeoutMock, readFileMock } = vi.hoisted(() => ({
  fetchWithTimeoutMock: vi.fn(),
  readFileMock: vi.fn(),
}));

vi.mock("node:fs/promises", () => ({
  default: { readFile: readFileMock },
  readFile: readFileMock,
}));
vi.mock("./fetchTimeout", () => ({
  fetchWithTimeout: (...args: unknown[]) => fetchWithTimeoutMock(...args),
  FetchTimeoutError: class FetchTimeoutError extends Error {},
}));

import { proxyAuthorized, proxyRead, requireSameOriginMutation } from "./serverProxy";

const HOP_SECRET = "caddy-injected-secret";
const REMOTE_ORIGIN = "https://mac-studio.tail0000.ts.net";

function mutation(headers: HeadersInit = {}, body?: BodyInit): Request {
  return new Request("https://dashboard.example.test/api/tasks", {
    method: "POST",
    headers: { "X-Omni-Trusted-Hop": HOP_SECRET, ...headers },
    body,
  });
}

describe("serverProxy same-origin / CSRF enforcement (S-B1)", () => {
  beforeEach(() => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP_SECRET);
    readFileMock.mockReset();
    readFileMock.mockResolvedValue("disk-only-session-token\n");
    fetchWithTimeoutMock.mockReset();
    fetchWithTimeoutMock.mockResolvedValue(
      new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  // --- (1) cross-site POST refused -----------------------------------------
  it("refuses a cross-site POST even with a valid hop header and a JSON body", async () => {
    const request = mutation(
      { "Sec-Fetch-Site": "cross-site", "Content-Type": "application/json" },
      "{}",
    );

    const response = await proxyAuthorized("/api/tasks", request, "POST");

    expect(response.status).toBe(403);
    const body = (await response.json()) as { error: { message: string } };
    expect(body.error.message).toMatch(/cross-origin mutation refused/i);
    // Denied before ever touching the disk-resident token or the upstream.
    expect(readFileMock).not.toHaveBeenCalled();
    expect(fetchWithTimeoutMock).not.toHaveBeenCalled();
  });

  it("also refuses a same-site (sibling subdomain) mutation — only same-origin/none pass", async () => {
    const request = mutation({ "Sec-Fetch-Site": "same-site", "Content-Type": "application/json" }, "{}");

    const response = await proxyAuthorized("/api/tasks", request, "POST");

    expect(response.status).toBe(403);
  });

  // --- (2) same-origin POST passes ------------------------------------------
  it("passes a same-origin POST through to the upstream", async () => {
    const request = mutation(
      { "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json" },
      "{}",
    );

    const response = await proxyAuthorized("/api/tasks", request, "POST");

    expect(response.status).toBe(200);
    expect(readFileMock).toHaveBeenCalledOnce();
    expect(fetchWithTimeoutMock).toHaveBeenCalledOnce();
  });

  // --- (3) remote-hostname origin passes ------------------------------------
  it("permits an allowlisted remote (Tailscale) hostname Origin with no Sec-Fetch-Site header", async () => {
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS", REMOTE_ORIGIN);
    const request = mutation({ Origin: REMOTE_ORIGIN, "Content-Type": "application/json" }, "{}");

    const response = await proxyAuthorized("/api/tasks", request, "POST");

    expect(response.status).toBe(200);
  });

  it("refuses an Origin that is not on the allowlist, even with no Sec-Fetch-Site header", async () => {
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS", REMOTE_ORIGIN);
    const request = mutation({ Origin: "https://evil.example", "Content-Type": "application/json" }, "{}");

    const response = await proxyAuthorized("/api/tasks", request, "POST");

    expect(response.status).toBe(403);
  });

  it("permits the loopback dashboard origin by default with no configuration", async () => {
    const request = mutation({ Origin: "http://localhost:3003", "Content-Type": "application/json" }, "{}");

    const response = await proxyAuthorized("/api/tasks", request, "POST");

    expect(response.status).toBe(200);
  });

  // --- review fix (polish item): "null" (a browser's literal Origin for an
  // opaque/sandboxed context — a sandboxed iframe, a data: URL) must never be
  // allowlistable, even by an operator's config typo/misconfiguration. -----
  it('refuses Origin: "null" even when an operator has (mis)configured "null" into the allowlist', async () => {
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_ALLOWED_ORIGINS", "null");
    const request = mutation({ Origin: "null", "Content-Type": "application/json" }, "{}");

    const response = await proxyAuthorized("/api/tasks", request, "POST");

    expect(response.status).toBe(403);
  });

  it('refuses a bare Origin: "null" with no configuration at all', async () => {
    const request = mutation({ Origin: "null", "Content-Type": "application/json" }, "{}");

    const response = await proxyAuthorized("/api/tasks", request, "POST");

    expect(response.status).toBe(403);
  });

  // --- (4) GET/SSE untouched -------------------------------------------------
  it("never applies to GET — including an EventSource-style SSE request carrying a cross-site signal", () => {
    const request = new Request("https://dashboard.example.test/api/events", {
      method: "GET",
      headers: { "Sec-Fetch-Site": "cross-site", Accept: "text/event-stream" },
    });

    expect(requireSameOriginMutation(request)).toBeNull();
  });

  it("proxyRead (the actual GET/SSE path) is unaffected end-to-end by a cross-site signal", async () => {
    fetchWithTimeoutMock.mockResolvedValue(new Response("[]", { status: 200 }));
    const request = new Request("https://dashboard.example.test/api/access/servers", {
      method: "GET",
      headers: { "X-Omni-Trusted-Hop": HOP_SECRET, "Sec-Fetch-Site": "cross-site" },
    });

    const response = await proxyRead("/api/access/servers", request);

    expect(response.status).toBe(200);
  });

  // --- (5) missing-Origin policy, exercised (see file-header doc comment) --
  it("DECISION: a mutation with neither Sec-Fetch-Site nor Origin is refused (fail closed)", async () => {
    const request = mutation({ "Content-Type": "application/json" }, "{}");

    const response = await proxyAuthorized("/api/tasks", request, "POST");

    expect(response.status).toBe(403);
    expect(readFileMock).not.toHaveBeenCalled();
  });

  // --- content-type laundering: stopped, not just relabeled -----------------
  it("415s a same-origin mutation whose content type is neither JSON nor multipart, and never reaches the upstream", async () => {
    const request = mutation(
      { "Sec-Fetch-Site": "same-origin", "Content-Type": "text/plain" },
      '{"sneaky":"json-shaped body riding a simple content type"}',
    );

    const response = await proxyAuthorized("/api/tasks", request, "POST");

    expect(response.status).toBe(415);
    expect(readFileMock).not.toHaveBeenCalled();
    expect(fetchWithTimeoutMock).not.toHaveBeenCalled();
  });

  it("passes the client's exact Content-Type upstream instead of relabeling it application/json", async () => {
    const request = mutation(
      { "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json; charset=utf-8" },
      '{"a":1}',
    );

    const response = await proxyAuthorized("/api/tasks", request, "POST");

    expect(response.status).toBe(200);
    const [, init] = fetchWithTimeoutMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json; charset=utf-8");
  });

  it("still forwards a multipart mutation with its boundary intact (regression)", async () => {
    const formData = new FormData();
    formData.append("file", new File(["hello"], "hello.txt", { type: "text/plain" }));
    const request = new Request("https://dashboard.example.test/api/tasks", {
      method: "POST",
      headers: { "X-Omni-Trusted-Hop": HOP_SECRET, "Sec-Fetch-Site": "same-origin" },
      body: formData,
    });

    const response = await proxyAuthorized("/api/tasks", request, "POST");

    expect(response.status).toBe(200);
    const [, init] = fetchWithTimeoutMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Content-Type")).toContain("multipart/form-data");
  });

  // --- review fix (blocker #1): the 415 check must parse the media-type
  // essence (everything before the first `;`, trimmed/lowercased) and require
  // EXACT equality — a prefix/substring check is bypassable. --------------
  describe("adversarial near-match content types (media-type essence, not substring/prefix)", () => {
    const shouldBeRefused: Array<[string, string]> = [
      [
        "a real type carrying a parameter whose VALUE merely contains the multipart string",
        "text/plain; note=multipart/form-data",
      ],
      ["application/json with trailing garbage after a space, no semicolon", "application/json garbage"],
      ["a longer type that merely starts with application/json", "application/jsonx"],
      ["a longer type that merely starts with multipart/form-data", "multipart/form-data-ish"],
      ["uppercase near-miss is still a near-miss", "APPLICATION/JSONX"],
      ["leading whitespace does not rescue a near-miss", "   application/jsonx"],
    ];

    it.each(shouldBeRefused)("415s: %s (%j)", async (_label, contentType) => {
      const request = mutation({ "Sec-Fetch-Site": "same-origin", "Content-Type": contentType }, "{}");

      const response = await proxyAuthorized("/api/tasks", request, "POST");

      expect(response.status).toBe(415);
      expect(fetchWithTimeoutMock).not.toHaveBeenCalled();
    });

    const shouldBeAccepted: Array<[string, string]> = [
      ["uppercase JSON essence", "APPLICATION/JSON"],
      ["mixed-case JSON essence with a charset parameter", "Application/Json; charset=UTF-8"],
      ["leading whitespace before a valid essence", "   application/json"],
      ["uppercase multipart essence with a boundary parameter", "MULTIPART/FORM-DATA; boundary=abc123"],
    ];

    it.each(shouldBeAccepted)("passes: %s (%j)", async (_label, contentType) => {
      const request = mutation({ "Sec-Fetch-Site": "same-origin", "Content-Type": contentType }, "{}");

      const response = await proxyAuthorized("/api/tasks", request, "POST");

      expect(response.status).toBe(200);
    });
  });
});
