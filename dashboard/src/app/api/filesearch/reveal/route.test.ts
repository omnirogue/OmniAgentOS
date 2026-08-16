import type { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyAuthorizedPostMock, proxyAuthorizedMock, proxyPublicReadMock } = vi.hoisted(() => ({
  proxyAuthorizedPostMock: vi.fn(() => Promise.resolve(new Response("authorized-post", { status: 202 }))),
  proxyAuthorizedMock: vi.fn(() => Promise.resolve(new Response("authorized", { status: 202 }))),
  proxyPublicReadMock: vi.fn(() => Promise.resolve(new Response("public", { status: 202 }))),
}));

vi.mock("@/lib/serverProxy", () => ({
  proxyAuthorizedPost: proxyAuthorizedPostMock,
  proxyAuthorized: proxyAuthorizedMock,
  proxyPublicRead: proxyPublicReadMock,
}));

import { POST } from "./route";

describe("POST /api/filesearch/reveal", () => {
  beforeEach(() => {
    proxyAuthorizedPostMock.mockClear();
    proxyAuthorizedMock.mockClear();
    proxyPublicReadMock.mockClear();
  });

  it("forwards the request through the token-bearing POST proxy", async () => {
    // The handler only ever hands the request straight to the proxy, so the
    // plain `Request` carries everything under assertion here. Cast to the
    // declared parameter type, as the other route tests in this directory do
    // (`[...path]/route.test.ts:31`, `auth/login/route.test.ts:20`,
    // `tasks/route.test.ts:23`) — building a real NextRequest would add
    // `cookies`/`nextUrl`/`page`/`ua` that nothing on this path reads.
    const request = new Request("http://dashboard.test/api/filesearch/reveal", {
      method: "POST",
      headers: { "content-type": "application/json", "x-test": "preserve-me" },
      body: JSON.stringify({ path: "/workspace/report.txt", app: "finder" }),
    }) as NextRequest;

    const response = await POST(request);

    expect(proxyAuthorizedPostMock).toHaveBeenCalledTimes(1);
    expect(proxyAuthorizedPostMock).toHaveBeenCalledWith("/api/filesearch/reveal", request);
    expect(proxyAuthorizedMock).not.toHaveBeenCalled();
    expect(proxyPublicReadMock).not.toHaveBeenCalled();
    expect(response.status).toBe(202);
  });
});
