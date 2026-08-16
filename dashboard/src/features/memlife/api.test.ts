/**
 * L7 API client: three queue payloads stay distinct after parse.
 * A 503 `{error: store_unavailable}` must never become `{pending: 0}`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchWithTimeout = vi.fn();

vi.mock("../../lib/fetchTimeout", () => ({
  fetchWithTimeout: (...args: unknown[]) => fetchWithTimeout(...args),
  FetchTimeoutError: class FetchTimeoutError extends Error {
    constructor(timeoutMs: number, input: string) {
      super(`Request to ${input} timed out after ${timeoutMs}ms`);
      this.name = "FetchTimeoutError";
    }
  },
}));

import {
  fetchMemlifeQueue,
  MemlifeApiError,
  parseQueueResponse,
} from "./api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("parseQueueResponse — three states", () => {
  it("maps 200 {pending: N} to ok with that count", () => {
    expect(parseQueueResponse(200, { pending: 4 })).toEqual({
      kind: "ok",
      pending: 4,
    });
  });

  it("maps 200 {pending: 0} to ok empty — only when status is 200", () => {
    expect(parseQueueResponse(200, { pending: 0 })).toEqual({
      kind: "ok",
      pending: 0,
    });
  });

  it("maps non-200 {error: store_unavailable} to store_unavailable", () => {
    expect(parseQueueResponse(503, { error: "store_unavailable" })).toEqual({
      kind: "store_unavailable",
    });
  });

  it("counterfeit: 503 store_unavailable must NOT parse as pending: 0", () => {
    const parsed = parseQueueResponse(503, { error: "store_unavailable" });
    expect(parsed.kind).toBe("store_unavailable");
    expect(parsed).not.toEqual({ kind: "ok", pending: 0 });
    if (parsed.kind === "ok") {
      throw new Error("COUNTERFEIT: store_unavailable collapsed into empty queue");
    }
  });

  it("refuses a 200 body that claims store_unavailable without a pending count", () => {
    // Absent pending on 200 is unparseable — not a flattering empty answer.
    const parsed = parseQueueResponse(200, { error: "store_unavailable" });
    expect(parsed.kind).not.toBe("ok");
  });
});

describe("fetchMemlifeQueue", () => {
  beforeEach(() => {
    fetchWithTimeout.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("returns ok pending N from a healthy queue", async () => {
    fetchWithTimeout.mockResolvedValue(jsonResponse({ pending: 2 }, 200));
    await expect(fetchMemlifeQueue()).resolves.toEqual({ kind: "ok", pending: 2 });
  });

  it("returns ok pending 0 from a true empty queue", async () => {
    fetchWithTimeout.mockResolvedValue(jsonResponse({ pending: 0 }, 200));
    await expect(fetchMemlifeQueue()).resolves.toEqual({ kind: "ok", pending: 0 });
  });

  it("returns store_unavailable on 503 without throwing a generic error", async () => {
    fetchWithTimeout.mockResolvedValue(
      jsonResponse({ error: "store_unavailable" }, 503),
    );
    await expect(fetchMemlifeQueue()).resolves.toEqual({
      kind: "store_unavailable",
    });
  });

  it("throws MemlifeApiError for other failures (still not empty)", async () => {
    fetchWithTimeout.mockResolvedValue(
      jsonResponse({ error: { message: "boom" } }, 500),
    );
    await expect(fetchMemlifeQueue()).rejects.toBeInstanceOf(MemlifeApiError);
  });
});
