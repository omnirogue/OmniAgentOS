import type { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyAuthorizedMock } = vi.hoisted(() => ({
  proxyAuthorizedMock: vi.fn(() => Promise.resolve(new Response("authorized", { status: 202 }))),
}));

vi.mock("@/lib/serverProxy", () => ({
  proxyAuthorized: proxyAuthorizedMock,
}));

import { POST } from "./route";

describe("POST /api/runs/{id}/cancel auth ownership", () => {
  beforeEach(() => {
    proxyAuthorizedMock.mockClear();
  });

  it("uses the authorized proxy and encodes a hostile run ID", async () => {
    const request = new Request("http://dashboard.test/api/runs/run-7/cancel", {
      method: "POST",
    }) as NextRequest;

    const response = await POST(request, {
      params: Promise.resolve({ id: "run/7?trace=1" }),
    });

    expect(proxyAuthorizedMock).toHaveBeenCalledTimes(1);
    expect(proxyAuthorizedMock).toHaveBeenCalledWith(
      "/api/runs/run%2F7%3Ftrace%3D1/cancel",
      request,
      "POST",
    );
    expect(response.status).toBe(202);
  });
});
