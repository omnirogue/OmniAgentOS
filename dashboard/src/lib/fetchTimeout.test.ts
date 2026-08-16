import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_FETCH_TIMEOUT_MS, FetchTimeoutError, fetchWithTimeout } from "./fetchTimeout";

function okResponse(): Response {
  return { ok: true, status: 200, statusText: "OK", json: async () => ({}) } as Response;
}

describe("fetchWithTimeout", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("rejects with FetchTimeoutError once the timeout elapses, instead of hanging forever", async () => {
    vi.useFakeTimers();
    // A fetch that never resolves/rejects on its own models a wedged backend
    // (hung DB connection, a process that still holds the TCP socket but
    // never answers, a network partition, ...) — exactly the failure mode
    // that, before this file existed, left a page's `loading` state stuck
    // `true` forever (every hook only clears `loading` inside a
    // `.then`/`.catch`/`finally` on the fetch promise, and a promise that
    // never settles never runs either). It DOES react to the AbortSignal,
    // same as a real `fetch` implementation — that reaction is exactly the
    // mechanism under test.
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_input: string, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => {
              reject(new DOMException("The operation was aborted.", "AbortError"));
            });
          }),
      ),
    );

    const pending = fetchWithTimeout("http://127.0.0.1:8485/api/revenue", undefined, 10_000);
    const assertion = expect(pending).rejects.toBeInstanceOf(FetchTimeoutError);

    await vi.advanceTimersByTimeAsync(10_000);
    await assertion;
  });

  it("does not time out a fetch that resolves well within the window", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okResponse()));

    const res = await fetchWithTimeout("http://127.0.0.1:8485/api/revenue", undefined, DEFAULT_FETCH_TIMEOUT_MS);
    expect(res.ok).toBe(true);
  });

  it("passes an AbortSignal through to fetch so a slow response is actually cancelled, not just ignored", async () => {
    const fetchSpy = vi.fn().mockResolvedValue(okResponse());
    vi.stubGlobal("fetch", fetchSpy);

    await fetchWithTimeout("http://127.0.0.1:8485/api/revenue");

    const [, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(init.signal?.aborted).toBe(false);
  });

  it("lets a genuine network failure (not a timeout) propagate as-is", async () => {
    const networkError = new TypeError("Failed to fetch");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(networkError));

    await expect(fetchWithTimeout("http://127.0.0.1:8485/api/revenue")).rejects.toBe(networkError);
  });
});
