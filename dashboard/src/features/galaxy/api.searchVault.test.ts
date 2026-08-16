import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * searchVault must not repeat the bug this file exists to close: a bare
 * `getJson()` that only returns the parsed body throws every response
 * header away, including `X-Vault-Search-Truncated`. Pin that the header
 * reaches a typed `{ hits, truncated }` result, in both directions.
 */

const { fetchWithTimeoutMock } = vi.hoisted(() => ({
  fetchWithTimeoutMock: vi.fn(),
}));

vi.mock("../../lib/fetchTimeout", () => ({
  fetchWithTimeout: (...args: unknown[]) => fetchWithTimeoutMock(...args),
  FetchTimeoutError: class FetchTimeoutError extends Error {},
}));

import { searchVault } from "./api";

function jsonResponse(body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

describe("searchVault", () => {
  beforeEach(() => {
    fetchWithTimeoutMock.mockReset();
  });

  it("returns truncated: true when the response header says so", async () => {
    fetchWithTimeoutMock.mockResolvedValue(
      jsonResponse(
        [{ path: "a.md", title: "A", type: "run", snippet: "…" }],
        { "X-Vault-Search-Truncated": "true" },
      ),
    );
    const result = await searchVault("needle");
    expect(result.hits).toHaveLength(1);
    expect(result.hits[0]!.path).toBe("a.md");
    expect(result.truncated).toBe(true);
  });

  it("returns truncated: false for a complete search", async () => {
    fetchWithTimeoutMock.mockResolvedValue(
      jsonResponse([], { "X-Vault-Search-Truncated": "false" }),
    );
    const result = await searchVault("needle");
    expect(result.truncated).toBe(false);
  });

  it("defaults truncated to false when the header is absent (never crashes on a missing signal)", async () => {
    fetchWithTimeoutMock.mockResolvedValue(jsonResponse([]));
    const result = await searchVault("needle");
    expect(result.truncated).toBe(false);
  });

  it("does not call the network for a blank query, and reports untruncated", async () => {
    const result = await searchVault("   ");
    expect(fetchWithTimeoutMock).not.toHaveBeenCalled();
    expect(result).toEqual({ hits: [], truncated: false });
  });
});
