import { beforeEach, describe, expect, it, vi } from "vitest";
import type { NextRequest } from "next/server";

/**
 * The catch-all proxy is the wire that makes server-side auth actually
 * matter: `GET /api/ledger/claims`/`GET /api/ledger/tail` are gated by
 * `require_session_token` in `omniagentos/api/main.py`, but the BROWSER
 * never holds that token -- it only reaches a gated route if THIS file
 * routes the request through `proxyRead` (which attaches it) instead of
 * `proxyPublicRead` (which does not). Getting this wrong is the exact class
 * of regression `tests/api/test_auth_posture.py`'s docstring names: "Gating
 * one of those without the matching dashboard change 401s a live page."
 */

const { proxyReadMock, proxyPublicReadMock, proxyAuthorizedMock } = vi.hoisted(() => ({
  proxyReadMock: vi.fn(() => Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))),
  proxyPublicReadMock: vi.fn(() => Promise.resolve(new Response(JSON.stringify([]), { status: 200 }))),
  proxyAuthorizedMock: vi.fn(() => Promise.resolve(new Response(JSON.stringify({}), { status: 200 }))),
}));

vi.mock("@/lib/serverProxy", () => ({
  proxyRead: proxyReadMock,
  proxyPublicRead: proxyPublicReadMock,
  proxyAuthorized: proxyAuthorizedMock,
}));

import { GET } from "./route";

function nextRequest(url: string): NextRequest {
  const parsed = new URL(url);
  const request = new Request(url) as NextRequest;
  Object.defineProperty(request, "nextUrl", { value: parsed, configurable: true });
  return request;
}

function ctx(path: string[]): { params: Promise<{ path: string[] }> } {
  return { params: Promise.resolve({ path }) };
}

describe("Catch-all API proxy — session-ledger read gating", () => {
  beforeEach(() => {
    proxyReadMock.mockClear();
    proxyPublicReadMock.mockClear();
    proxyAuthorizedMock.mockClear();
  });

  it("attaches the session token for GET /api/ledger/claims", async () => {
    const request = nextRequest("http://dashboard.test/api/ledger/claims");

    await GET(request, ctx(["ledger", "claims"]));

    expect(proxyReadMock).toHaveBeenCalledWith("/api/ledger/claims", request);
    expect(proxyPublicReadMock).not.toHaveBeenCalled();
  });

  it("attaches the session token for GET /api/ledger/tail, query string intact", async () => {
    const request = nextRequest("http://dashboard.test/api/ledger/tail?ref=task%3Dbtk_1&n=50");

    await GET(request, ctx(["ledger", "tail"]));

    expect(proxyReadMock).toHaveBeenCalledWith("/api/ledger/tail?ref=task%3Dbtk_1&n=50", request);
    expect(proxyPublicReadMock).not.toHaveBeenCalled();
  });

  it("leaves the unrelated public GET /api/ledger ungated -- a literal two-path match, not the whole prefix", async () => {
    const request = nextRequest("http://dashboard.test/api/ledger");

    await GET(request, ctx(["ledger"]));

    expect(proxyPublicReadMock).toHaveBeenCalledWith("/api/ledger", request);
    expect(proxyReadMock).not.toHaveBeenCalled();
  });

  it("does not gate an unrelated single-segment path that merely starts with the same prefix letter", async () => {
    // Sanity check on the length===2 guard: something like /api/ledgerx
    // (a single, unrelated segment) must not accidentally match.
    const request = nextRequest("http://dashboard.test/api/ledgerx");

    await GET(request, ctx(["ledgerx"]));

    expect(proxyPublicReadMock).toHaveBeenCalledWith("/api/ledgerx", request);
    expect(proxyReadMock).not.toHaveBeenCalled();
  });

  it("does not gate an unrelated TWO-segment ledger sub-path -- the sloppiness case (literal leaf match, not a prefix match)", async () => {
    // Pins the case a broader `path[0] === "ledger"` check (matching the
    // AUTHORIZED_READ_PREFIXES whole-namespace style used elsewhere in this
    // file) would have swept in by mistake: a real two-segment path under
    // /api/ledger/* whose leaf is neither "claims" nor "tail" must stay
    // UNAUTHORIZED through the proxy -- routed via proxyPublicRead, same as
    // the bare /api/ledger route, never proxyRead.
    const request = nextRequest("http://dashboard.test/api/ledger/anything-else");

    await GET(request, ctx(["ledger", "anything-else"]));

    expect(proxyPublicReadMock).toHaveBeenCalledWith("/api/ledger/anything-else", request);
    expect(proxyReadMock).not.toHaveBeenCalled();
  });

  it("attaches the session token for employee transcript metadata reads", async () => {
    const request = nextRequest("http://dashboard.test/api/employee-transcripts/tr_123");

    await GET(request, ctx(["employee-transcripts", "tr_123"]));

    expect(proxyReadMock).toHaveBeenCalledWith("/api/employee-transcripts/tr_123", request);
    expect(proxyPublicReadMock).not.toHaveBeenCalled();
  });

  it.each([
    [
      "project files",
      "/api/projects/project-1/files/src/app.ts",
      ["projects", "project-1", "files", "src", "app.ts"],
    ],
    ["mounts", "/api/mounts/home", ["mounts", "home"]],
    ["workfs", "/api/workfs/tree", ["workfs", "tree"]],
    ["server inventory", "/api/access/servers", ["access", "servers"]],
    ["accounts", "/api/accounts/usage", ["accounts", "usage"]],
    ["provision", "/api/provision/project-1", ["provision", "project-1"]],
  ])("attaches the session token for %s cross-principal reads", async (_label, url, path) => {
    const request = nextRequest(`http://dashboard.test${url}`);

    await GET(request, ctx(path as string[]));

    expect(proxyReadMock).toHaveBeenCalledWith(url, request);
    expect(proxyPublicReadMock).not.toHaveBeenCalled();
  });
});
