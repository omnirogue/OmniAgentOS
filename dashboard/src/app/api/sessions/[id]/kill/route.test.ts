import type { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyAuthorizedPostMock } = vi.hoisted(() => ({
  proxyAuthorizedPostMock: vi.fn(() => Promise.resolve(new Response("killed", { status: 202 }))),
}));

vi.mock("@/lib/serverProxy", () => ({
  proxyAuthorizedPost: proxyAuthorizedPostMock,
}));

import { POST } from "./route";

describe("POST /api/sessions/{id}/kill auth ownership", () => {
  beforeEach(() => {
    proxyAuthorizedPostMock.mockClear();
  });

  it("uses the token-bearing proxy and encodes the session ID", async () => {
    const request = new Request("http://dashboard.test/api/sessions/session-1/kill", {
      method: "POST",
    }) as NextRequest;

    const response = await POST(request, {
      params: Promise.resolve({ id: "session/1?next=/admin" }),
    });

    expect(proxyAuthorizedPostMock).toHaveBeenCalledTimes(1);
    expect(proxyAuthorizedPostMock).toHaveBeenCalledWith(
      "/api/sessions/session%2F1%3Fnext%3D%2Fadmin/kill",
      request,
    );
    expect(response.status).toBe(202);
  });
});
