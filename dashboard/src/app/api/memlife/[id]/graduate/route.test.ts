import type { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyAuthorizedPostMock } = vi.hoisted(() => ({
  proxyAuthorizedPostMock: vi.fn(() =>
    Promise.resolve(new Response("graduated", { status: 202 })),
  ),
}));

vi.mock("@/lib/serverProxy", () => ({
  proxyAuthorizedPost: proxyAuthorizedPostMock,
}));

import { POST } from "./route";

describe("POST /api/memlife/{id}/graduate auth ownership", () => {
  beforeEach(() => {
    proxyAuthorizedPostMock.mockClear();
  });

  it("uses the authorized proxy and encodes a hostile candidate ID", async () => {
    const request = new Request("http://dashboard.test/api/memlife/candidate-7/graduate", {
      method: "POST",
    }) as NextRequest;

    const response = await POST(request, {
      params: Promise.resolve({ id: "candidate/7?next=/admin" }),
    });

    expect(proxyAuthorizedPostMock).toHaveBeenCalledTimes(1);
    expect(proxyAuthorizedPostMock).toHaveBeenCalledWith(
      "/api/memlife/candidate%2F7%3Fnext%3D%2Fadmin/graduate",
      request,
    );
    expect(response.status).toBe(202);
    expect(await response.text()).toBe("graduated");
  });
});
