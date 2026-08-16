import { beforeEach, describe, expect, it, vi } from "vitest";

const { fetchWithTimeoutMock } = vi.hoisted(() => ({
  fetchWithTimeoutMock: vi.fn(),
}));

vi.mock("./fetchTimeout", () => ({
  fetchWithTimeout: fetchWithTimeoutMock,
  FetchTimeoutError: class FetchTimeoutError extends Error {},
}));

import { api } from "./api";

describe("dashboard API paths", () => {
  beforeEach(() => {
    fetchWithTimeoutMock.mockReset();
    fetchWithTimeoutMock.mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });

  it("encodes approval IDs before placing them in the decision route", async () => {
    await api.decideApproval("approval/id?next=/admin", "approved");

    expect(fetchWithTimeoutMock).toHaveBeenCalledWith(
      "/api/approvals/approval%2Fid%3Fnext%3D%2Fadmin/decision",
      expect.objectContaining({ method: "POST" }),
    );
  });
});
