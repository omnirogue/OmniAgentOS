import type { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyAuthorizedMock, proxyPublicReadMock } = vi.hoisted(() => ({
  proxyAuthorizedMock: vi.fn(() => Promise.resolve(new Response("authorized", { status: 202 }))),
  proxyPublicReadMock: vi.fn(() => Promise.resolve(new Response("public", { status: 200 }))),
}));

vi.mock("@/lib/serverProxy", () => ({
  proxyAuthorized: proxyAuthorizedMock,
  proxyPublicRead: proxyPublicReadMock,
}));

import { GET, PUT } from "./route";

function nextRequest(method: string): NextRequest {
  return new Request("http://dashboard.test/api/pause", { method }) as NextRequest;
}

describe("pause API route auth ownership", () => {
  beforeEach(() => {
    proxyAuthorizedMock.mockClear();
    proxyPublicReadMock.mockClear();
  });

  it("keeps the pause status read public", async () => {
    const request = nextRequest("GET");

    const response = await GET(request);

    expect(proxyPublicReadMock).toHaveBeenCalledWith("/api/pause", request);
    expect(proxyAuthorizedMock).not.toHaveBeenCalled();
    expect(response.status).toBe(200);
  });

  it("routes pause mutations through the authorized proxy", async () => {
    const request = nextRequest("PUT");

    const response = await PUT(request);

    expect(proxyAuthorizedMock).toHaveBeenCalledWith("/api/pause", request, "PUT");
    expect(proxyPublicReadMock).not.toHaveBeenCalled();
    expect(response.status).toBe(202);
  });
});
