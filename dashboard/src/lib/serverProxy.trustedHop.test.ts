import { beforeEach, describe, expect, it, vi } from "vitest";

const { fetchWithTimeoutMock, readFileMock } = vi.hoisted(() => ({
  fetchWithTimeoutMock: vi.fn(),
  readFileMock: vi.fn(),
}));

vi.mock("node:fs/promises", () => ({
  default: { readFile: readFileMock },
  readFile: readFileMock,
}));
vi.mock("./fetchTimeout", () => ({
  fetchWithTimeout: (...args: unknown[]) => fetchWithTimeoutMock(...args),
  FetchTimeoutError: class FetchTimeoutError extends Error {},
}));

import { proxyAuthorized, proxyRead, requireTrustedHop } from "./serverProxy";

function request(headers?: HeadersInit): Request {
  return new Request("https://dashboard.example.test/api/access/servers", { headers });
}

describe("serverProxy trusted-hop boundary", () => {
  beforeEach(() => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", "caddy-injected-secret");
    readFileMock.mockReset();
    readFileMock.mockResolvedValue("disk-only-session-token\n");
    fetchWithTimeoutMock.mockReset();
    fetchWithTimeoutMock.mockResolvedValue(new Response("{}", { status: 200 }));
  });

  it("refuses a missing or forged hop secret with one indistinguishable 403", async () => {
    const missing = requireTrustedHop(request());
    const forged = requireTrustedHop(request({ "X-Omni-Trusted-Hop": "forged" }));

    expect(missing?.status).toBe(403);
    expect(forged?.status).toBe(403);
    expect(await missing?.text()).toBe(await forged?.text());
  });

  it("refuses an authorized mutation before reading the session token", async () => {
    const response = await proxyAuthorized("/api/autonomy", request(), "PUT");

    expect(response.status).toBe(403);
    expect(readFileMock).not.toHaveBeenCalled();
    expect(fetchWithTimeoutMock).not.toHaveBeenCalled();
  });

  it("refuses a gated read with a forged hop header before reading the session token", async () => {
    const response = await proxyRead(
      "/api/access/servers",
      request({ "X-Omni-Trusted-Hop": "forged" }),
    );

    expect(response.status).toBe(403);
    expect(readFileMock).not.toHaveBeenCalled();
    expect(fetchWithTimeoutMock).not.toHaveBeenCalled();
  });

  it("injects the disk-only token only after the trusted hop validates", async () => {
    const response = await proxyRead(
      "/api/access/servers",
      request({ "X-Omni-Trusted-Hop": "caddy-injected-secret" }),
    );

    expect(readFileMock).toHaveBeenCalledOnce();
    expect(response.status).toBe(200);
    const [, init] = fetchWithTimeoutMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("X-Session-Token")).toBe("disk-only-session-token");
  });
});
