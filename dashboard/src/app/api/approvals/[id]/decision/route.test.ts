import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyAuthorizedPostMock } = vi.hoisted(() => ({
  proxyAuthorizedPostMock: vi.fn(() =>
    Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 })),
  ),
}));

vi.mock("@/lib/serverProxy", () => ({
  proxyAuthorizedPost: proxyAuthorizedPostMock,
}));

import type { NextRequest } from "next/server";
import { POST } from "./route";

function nextRequest(url: string): NextRequest {
  return new Request(url, { method: "POST" }) as NextRequest;
}

describe("approval decision API route", () => {
  beforeEach(() => {
    proxyAuthorizedPostMock.mockClear();
  });

  it("encodes the decision ID before handing it to the authorized proxy", async () => {
    const request = nextRequest("http://dashboard.test/api/approvals/decision");
    const response = await POST(request, {
      params: Promise.resolve({ id: "approval/id?next=/admin" }),
    });

    expect(proxyAuthorizedPostMock).toHaveBeenCalledWith(
      "/api/approvals/approval%2Fid%3Fnext%3D%2Fadmin/decision",
      request,
    );
    expect(response.status).toBe(200);
  });
});
