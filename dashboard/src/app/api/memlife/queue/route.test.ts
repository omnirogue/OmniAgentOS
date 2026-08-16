import type { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyPublicReadMock, proxyReadMock } = vi.hoisted(() => ({
  proxyPublicReadMock: vi.fn(() => Promise.resolve(new Response("upstream", { status: 503 }))),
  proxyReadMock: vi.fn(() => Promise.resolve(new Response("token-bearing", { status: 200 }))),
}));

vi.mock("@/lib/serverProxy", () => ({
  proxyPublicRead: proxyPublicReadMock,
  proxyRead: proxyReadMock,
}));

import { GET } from "./route";

describe("GET /api/memlife/queue proxy ownership", () => {
  beforeEach(() => {
    proxyPublicReadMock.mockClear();
    proxyReadMock.mockClear();
  });

  it("uses the public read proxy and relays the upstream status", async () => {
    const request = new Request("http://dashboard.test/api/memlife/queue") as NextRequest;

    const response = await GET(request);

    expect(proxyPublicReadMock).toHaveBeenCalledTimes(1);
    expect(proxyPublicReadMock).toHaveBeenCalledWith("/api/memlife/queue", request);
    expect(proxyReadMock).not.toHaveBeenCalled();
    expect(response.status).toBe(503);
    expect(await response.text()).toBe("upstream");
  });
});
