import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BROWSER_CREDENTIAL_COOKIE, mintBrowserCredential } from "./browserCredential";

const SESSION_TOKEN = "disk-only-session-token";
const AUTONOMY_TOKEN = "disk-only-autonomy-token";
const HOP_SECRET = "caddy-injected-secret";
const PRINCIPAL_HEADER = "X-Omni-Authenticated-Principal";
const OPERATOR = "operator@example.test";

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

import { proxyAuthorized, proxyPublicRead, proxyRead } from "./serverProxy";

type RouteCase = {
  label: string;
  invoke: (headers?: HeadersInit) => Promise<Response>;
};

function mutation(path: string, method: "POST" | "PUT", headers: HeadersInit = {}): Request {
  const merged = new Headers(headers);
  merged.set("X-Omni-Trusted-Hop", HOP_SECRET);
  merged.set("Sec-Fetch-Site", "same-origin");
  merged.set("Content-Type", "application/json");
  return new Request(`https://dashboard.example.test${path}`, {
    method,
    headers: merged,
    body: "{}",
  });
}

function read(path: string, headers: HeadersInit = {}): Request {
  const merged = new Headers(headers);
  merged.set("X-Omni-Trusted-Hop", HOP_SECRET);
  return new Request(`https://dashboard.example.test${path}`, { headers: merged });
}

function signedCookie(principal = OPERATOR): string {
  const credential = mintBrowserCredential(principal, SESSION_TOKEN);
  return `${BROWSER_CREDENTIAL_COOKIE}=${credential}`;
}

function upstreamHeaders(): Headers {
  expect(fetchWithTimeoutMock).toHaveBeenCalledOnce();
  const [, init] = fetchWithTimeoutMock.mock.calls[0] as [string, RequestInit];
  return new Headers(init.headers);
}

const routes: RouteCase[] = [
  {
    label: "second_factor PUT /api/autonomy",
    invoke: (headers) =>
      proxyAuthorized("/api/autonomy", mutation("/api/autonomy", "PUT", headers), "PUT", {
        autonomy: true,
      }),
  },
  {
    label: "approval_parked POST /api/approvals/{id}/decision",
    invoke: (headers) =>
      proxyAuthorized(
        "/api/approvals/apr-1/decision",
        mutation("/api/approvals/apr-1/decision", "POST", headers),
        "POST",
      ),
  },
  {
    label: "cross_principal_read GET /api/access/servers",
    invoke: (headers) =>
      proxyRead("/api/access/servers", read("/api/access/servers", headers)),
  },
];

describe("serverProxy signed browser-credential principal forwarding", () => {
  beforeEach(() => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", HOP_SECRET);
    vi.stubEnv("OMNIAGENTOS_TOKEN_PATH", "/test/session-token");
    vi.stubEnv("OMNIAGENTOS_AUTONOMY_TOKEN_PATH", "/test/autonomy-token");
    readFileMock.mockReset();
    readFileMock.mockImplementation(async (path: string) =>
      path === "/test/autonomy-token" ? `${AUTONOMY_TOKEN}\n` : `${SESSION_TOKEN}\n`,
    );
    fetchWithTimeoutMock.mockReset();
    fetchWithTimeoutMock.mockResolvedValue(
      new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it.each(routes)("forwards a valid signed principal for $label", async ({ invoke }) => {
    const response = await invoke({ Cookie: signedCookie() });

    expect(response.status).toBe(200);
    expect(upstreamHeaders().get(PRINCIPAL_HEADER)).toBe(OPERATOR);
  });

  it.each(routes)("omits the principal header without a credential for $label", async ({ invoke }) => {
    const response = await invoke();

    expect(response.status).toBe(200);
    expect(upstreamHeaders().has(PRINCIPAL_HEADER)).toBe(false);
    expect(upstreamHeaders().get(PRINCIPAL_HEADER)).toBeNull();
  });

  it.each(routes)("never forwards an inbound spoof for $label", async ({ invoke }) => {
    const response = await invoke({
      Cookie: signedCookie(),
      [PRINCIPAL_HEADER]: "attacker",
    });

    expect(response.status).toBe(200);
    expect(upstreamHeaders().get(PRINCIPAL_HEADER)).toBe(OPERATOR);
    expect(upstreamHeaders().get(PRINCIPAL_HEADER)).not.toBe("attacker");
  });

  it("drops an inbound spoof entirely when no signed credential resolves", async () => {
    const response = await proxyRead(
      "/api/access/servers",
      read("/api/access/servers", { [PRINCIPAL_HEADER]: "attacker" }),
    );

    expect(response.status).toBe(200);
    expect(upstreamHeaders().has(PRINCIPAL_HEADER)).toBe(false);
  });

  it("treats duplicate browser credential cookies as ambiguous", async () => {
    const cookie = signedCookie();
    const response = await proxyRead(
      "/api/access/servers",
      read("/api/access/servers", { Cookie: `${cookie}; ${cookie}` }),
    );

    expect(response.status).toBe(200);
    expect(upstreamHeaders().has(PRINCIPAL_HEADER)).toBe(false);
  });

  it("keeps public reads principal-free even when a valid credential cookie is present", async () => {
    const request = read("/api/ledger", { Cookie: signedCookie() });
    const response = await proxyPublicRead("/api/ledger", request);

    expect(response.status).toBe(200);
    expect(upstreamHeaders().has("X-Session-Token")).toBe(false);
    expect(upstreamHeaders().has(PRINCIPAL_HEADER)).toBe(false);
  });
});
