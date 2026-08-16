import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Local development authentication belongs in Edge middleware, where it can
 * turn a browser-native credential into the private hop assertion. The Node
 * proxy guard must never have its own no-hop exception: a direct caller must
 * remain unable to read the disk token or reach the upstream.
 */

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

function mutation(headers: HeadersInit = {}, body?: BodyInit): Request {
  return new Request("https://dashboard.example.test/api/tasks", {
    method: "POST",
    headers,
    body,
  });
}

describe("serverProxy trusted-hop guard remains fail-closed during local development", () => {
  beforeEach(() => {
    vi.stubEnv("OMNIAGENTOS_TRUSTED_HOP_SECRET", "caddy-injected-secret");
    readFileMock.mockReset();
    readFileMock.mockResolvedValue("disk-only-session-token\n");
    fetchWithTimeoutMock.mockReset();
    fetchWithTimeoutMock.mockResolvedValue(
      new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("refuses a no-hop read even when local development is explicitly enabled", async () => {
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP", "1");

    expect(requireTrustedHop(request())?.status).toBe(403);
    const response = await proxyRead("/api/access/servers", request());
    expect(response.status).toBe(403);
    expect(readFileMock).not.toHaveBeenCalled();
    expect(fetchWithTimeoutMock).not.toHaveBeenCalled();
  });

  it("refuses a no-hop same-origin mutation before reading a token", async () => {
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP", "1");

    const response = await proxyAuthorized(
      "/api/tasks",
      mutation({ "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json" }, "{}"),
      "POST",
    );

    expect(response.status).toBe(403);
    expect(readFileMock).not.toHaveBeenCalled();
    expect(fetchWithTimeoutMock).not.toHaveBeenCalled();
  });

  it("accepts only the assertion created by Caddy or authenticated middleware", () => {
    vi.stubEnv("NODE_ENV", "development");
    vi.stubEnv("OMNIAGENTOS_DASHBOARD_DEV_ALLOW_NO_HOP", "1");

    expect(requireTrustedHop(request({ "X-Omni-Trusted-Hop": "forged" }))?.status).toBe(403);
    expect(requireTrustedHop(request({ "X-Omni-Trusted-Hop": "caddy-injected-secret" }))).toBeNull();
  });
});
