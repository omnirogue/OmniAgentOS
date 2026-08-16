import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * GET /api/lab/vault/search signals an incomplete result on
 * `X-Vault-Search-Truncated`. Before this test existed the header was
 * silently stripped by the proxy's fixed relay allowlist: the FastAPI route
 * set it correctly, but proxyPublicRead (the relay every same-origin
 * `/api/lab/**` GET goes through) never carried it to the browser, so a
 * truncated search read as complete downstream. Pin that it now survives
 * the relay hop, both when present and when absent.
 */

const { fetchWithTimeoutMock } = vi.hoisted(() => ({
  fetchWithTimeoutMock: vi.fn(),
}));

vi.mock("./fetchTimeout", () => ({
  fetchWithTimeout: (...args: unknown[]) => fetchWithTimeoutMock(...args),
  FetchTimeoutError: class FetchTimeoutError extends Error {},
}));

import { proxyPublicRead } from "./serverProxy";

function upstream(status: number, headers: Record<string, string>, body: string | null = null) {
  return new Response(body, { status, headers });
}

function read(path: string): Request {
  return new Request(`http://dashboard.test${path}`);
}

describe("serverProxy relays X-Vault-Search-Truncated", () => {
  beforeEach(() => {
    fetchWithTimeoutMock.mockReset();
  });

  it("carries the header through when the upstream search was truncated", async () => {
    fetchWithTimeoutMock.mockResolvedValue(
      upstream(200, { "Content-Type": "application/json", "X-Vault-Search-Truncated": "true" }, "[]"),
    );
    const response = await proxyPublicRead("/api/lab/vault/search?q=needle", read("/api/lab/vault/search?q=needle"));
    expect(response.headers.get("X-Vault-Search-Truncated")).toBe("true");
  });

  it("carries the header through as false for a complete search", async () => {
    fetchWithTimeoutMock.mockResolvedValue(
      upstream(200, { "Content-Type": "application/json", "X-Vault-Search-Truncated": "false" }, "[]"),
    );
    const response = await proxyPublicRead("/api/lab/vault/search?q=needle", read("/api/lab/vault/search?q=needle"));
    expect(response.headers.get("X-Vault-Search-Truncated")).toBe("false");
  });

  it("does not invent the header when the upstream never sent one", async () => {
    fetchWithTimeoutMock.mockResolvedValue(upstream(200, { "Content-Type": "application/json" }, "[]"));
    const response = await proxyPublicRead("/api/board", read("/api/board"));
    expect(response.headers.get("X-Vault-Search-Truncated")).toBeNull();
  });
});
