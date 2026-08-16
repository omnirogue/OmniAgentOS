import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * A conditional response is a function of the REQUEST's `If-None-Match`, not of
 * the URL alone: the same URL answers 200-with-body or 304-with-none depending
 * on the validator the client sent. A cache that does not know that is entitled
 * to store the 304 under the URL and replay it to a client that sent no
 * validator and holds no cached copy — which lands as an empty body it cannot
 * recover from. `Vary: If-None-Match` and `Cache-Control: no-cache` are what
 * say so on the wire.
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
  return new Response(status === 304 ? null : body, { status, headers });
}

function read(path = "/api/board", init?: RequestInit): Request {
  return new Request(`http://dashboard.test${path}`, init);
}

describe("serverProxy conditional responses", () => {
  beforeEach(() => {
    fetchWithTimeoutMock.mockReset();
  });

  it("marks an ETagged 200 as varying on the validator and revalidate-only", async () => {
    fetchWithTimeoutMock.mockResolvedValue(
      upstream(200, { "Content-Type": "application/json", ETag: 'W/"board-abc"' }, "[]"),
    );
    const response = await proxyPublicRead("/api/board", read());
    expect(response.status).toBe(200);
    expect(response.headers.get("ETag")).toBe('W/"board-abc"');
    expect(response.headers.get("Vary")).toBe("If-None-Match");
    expect(response.headers.get("Cache-Control")).toBe("no-cache");
  });

  it("marks a 304 the same way, and still returns no body", async () => {
    fetchWithTimeoutMock.mockResolvedValue(upstream(304, { ETag: 'W/"board-abc"' }));
    const response = await proxyPublicRead(
      "/api/board",
      read("/api/board", { headers: { "If-None-Match": 'W/"board-abc"' } }),
    );
    expect(response.status).toBe(304);
    expect(await response.text()).toBe("");
    expect(response.headers.get("Vary")).toBe("If-None-Match");
    expect(response.headers.get("Cache-Control")).toBe("no-cache");
  });

  it("leaves a non-conditional read's caching exactly as the API asked", async () => {
    fetchWithTimeoutMock.mockResolvedValue(
      upstream(
        200,
        { "Content-Type": "image/png", "Cache-Control": "public, max-age=3600" },
        "bytes",
      ),
    );
    const response = await proxyPublicRead("/api/files/logo.png", read("/api/files/logo.png"));
    expect(response.headers.get("Cache-Control")).toBe("public, max-age=3600");
    expect(response.headers.get("Vary")).toBeNull();
  });

  it("never weakens an upstream directive it is adding to", async () => {
    fetchWithTimeoutMock.mockResolvedValue(
      upstream(200, { ETag: 'W/"x"', "Cache-Control": "no-store" }, "[]"),
    );
    const response = await proxyPublicRead("/api/board", read());
    expect(response.headers.get("Cache-Control")).toBe("no-store");
  });

  it("prepends no-cache to a cacheable directive on a conditional response", async () => {
    fetchWithTimeoutMock.mockResolvedValue(
      upstream(200, { ETag: 'W/"x"', "Cache-Control": "max-age=60" }, "[]"),
    );
    const response = await proxyPublicRead("/api/board", read());
    expect(response.headers.get("Cache-Control")).toBe("no-cache, max-age=60");
  });

  it("merges into an upstream Vary instead of replacing it", async () => {
    fetchWithTimeoutMock.mockResolvedValue(
      upstream(200, { ETag: 'W/"x"', Vary: "Accept-Encoding" }, "[]"),
    );
    const response = await proxyPublicRead("/api/board", read());
    expect(response.headers.get("Vary")).toBe("Accept-Encoding, If-None-Match");
  });

  it("does not duplicate a validator the API already declared", async () => {
    fetchWithTimeoutMock.mockResolvedValue(
      upstream(200, { ETag: 'W/"x"', Vary: "if-none-match" }, "[]"),
    );
    const response = await proxyPublicRead("/api/board", read());
    expect(response.headers.get("Vary")).toBe("if-none-match");
  });

  it("still forwards the client's validator upstream", async () => {
    fetchWithTimeoutMock.mockResolvedValue(upstream(304, { ETag: 'W/"x"' }));
    await proxyPublicRead(
      "/api/board",
      read("/api/board", { headers: { "If-None-Match": 'W/"x"' } }),
    );
    const [, init] = fetchWithTimeoutMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("If-None-Match")).toBe('W/"x"');
  });
});
