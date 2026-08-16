import type { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyAuthorizedPostMock } = vi.hoisted(() => ({
  proxyAuthorizedPostMock: vi.fn(() => Promise.resolve(new Response("authorized-post", { status: 202 }))),
}));

vi.mock("@/lib/serverProxy", () => ({
  proxyAuthorizedPost: proxyAuthorizedPostMock,
}));

import { POST } from "./route";

describe("POST /api/board/{id}/files/reveal auth ownership", () => {
  beforeEach(() => {
    proxyAuthorizedPostMock.mockClear();
  });

  it("uses the authorized token-bearing proxy and encodes the board ID", async () => {
    const request = new Request("http://dashboard.test/api/board/card/42/files/reveal", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ path: "/workspace/report.txt", app: "finder" }),
    }) as NextRequest;

    const response = await POST(request, {
      params: Promise.resolve({ id: "card/42?trace=1" }),
    });

    expect(proxyAuthorizedPostMock).toHaveBeenCalledTimes(1);
    expect(proxyAuthorizedPostMock).toHaveBeenCalledWith(
      "/api/board/card%2F42%3Ftrace%3D1/files/reveal",
      request,
    );
    expect(response.status).toBe(202);
  });
});
