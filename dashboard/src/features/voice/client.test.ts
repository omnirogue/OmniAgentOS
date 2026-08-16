import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MAX_TASK_TITLE_LENGTH, voiceDispatchApi } from "./client";

function jsonResponse(body: unknown, init?: { ok?: boolean; status?: number; statusText?: string }) {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: init?.statusText ?? "OK",
    json: async () => body,
  } as Response;
}

describe("voiceDispatchApi client", () => {
  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("requests disciplines same-origin through the Next.js proxy", async () => {
    // apiUrl (SEC-005 / AC-policy fix7) routes ALL requests same-origin so the
    // proxy injects the session token and reads stay off the EVENTS_BASE SSE pool.
    fetchSpy.mockResolvedValueOnce(jsonResponse([{ id: "d1", name: "Proj", status: "active" }]));
    const rows = await voiceDispatchApi.disciplines();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url] = fetchSpy.mock.calls[0]!;
    expect(url).toBe("/api/disciplines");
    expect(rows).toEqual([{ id: "d1", name: "Proj", status: "active" }]);
  });

  it("serializes createTask with title, discipline_id, and voice-intake input", async () => {
    fetchSpy.mockResolvedValueOnce(
      jsonResponse({ id: "t1", title: "hello", state: "queued", discipline_id: "d1" }),
    );
    await voiceDispatchApi.createTask({ title: "hello", disciplineId: "d1" });
    const [url, init] = fetchSpy.mock.calls[0]!;
    // fix6: the create route is token-gated, so createTask goes SAME-ORIGIN to
    // the Next.js proxy (no API_BASE host) which injects the session token.
    expect(url).toBe("/api/tasks");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({
      title: "hello",
      discipline_id: "d1",
      input: { text: "hello", source: "voice-intake" },
    });
  });

  it("omits discipline_id from the request body when not provided", async () => {
    fetchSpy.mockResolvedValueOnce(jsonResponse({ id: "t1", title: "hello", state: "queued", discipline_id: null }));
    await voiceDispatchApi.createTask({ title: "hello" });
    const [, init] = fetchSpy.mock.calls[0]!;
    const body = JSON.parse(init.body as string);
    expect(body.discipline_id).toBeUndefined();
    expect("discipline_id" in body).toBe(false);
  });

  it("encodeURIComponent-safely builds the createRun path from an arbitrary task id", async () => {
    fetchSpy.mockResolvedValueOnce(jsonResponse({ id: "r1", task_id: "weird/id?x=1", harness: "mock", state: "queued" }));
    await voiceDispatchApi.createRun("weird/id?x=1", "mock", "do it");
    const [url, init] = fetchSpy.mock.calls[0]!;
    // fix6: same-origin proxy path (no API_BASE host) — the token is attached
    // server-side by the Next.js route handler.
    expect(url).toBe("/api/tasks/weird%2Fid%3Fx%3D1/runs");
    const body = JSON.parse(init.body as string);
    expect(body).toEqual({ harness: "mock", prompt: "do it" });
  });

  it("throws with the extracted error.message when the response is non-ok JSON", async () => {
    fetchSpy.mockResolvedValueOnce(
      jsonResponse({ error: { message: "title is required" } }, { ok: false, status: 422, statusText: "Unprocessable Entity" }),
    );
    await expect(voiceDispatchApi.createTask({ title: "" })).rejects.toThrow("422: title is required");
  });

  it("falls back to a top-level message field when error.message is absent", async () => {
    fetchSpy.mockResolvedValueOnce(
      jsonResponse({ message: "bad request" }, { ok: false, status: 400, statusText: "Bad Request" }),
    );
    await expect(voiceDispatchApi.disciplines()).rejects.toThrow("400: bad request");
  });

  it("falls back to statusText when the error body isn't valid JSON", async () => {
    const res = {
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: async () => {
        throw new SyntaxError("Unexpected token < in JSON");
      },
    } as unknown as Response;
    fetchSpy.mockResolvedValueOnce(res);
    await expect(voiceDispatchApi.disciplines()).rejects.toThrow("500: Internal Server Error");
  });

  it("truncates an overly long error message instead of dumping the raw body", async () => {
    const huge = "x".repeat(5000);
    fetchSpy.mockResolvedValueOnce(
      jsonResponse({ error: { message: huge } }, { ok: false, status: 500, statusText: "Internal Server Error" }),
    );
    try {
      await voiceDispatchApi.disciplines();
      throw new Error("expected rejection");
    } catch (err) {
      const message = (err as Error).message;
      expect(message.length).toBeLessThan(250);
      expect(message).toContain("…");
    }
  });

  it("exports a MAX_TASK_TITLE_LENGTH matching the backend contract (2000)", () => {
    expect(MAX_TASK_TITLE_LENGTH).toBe(2000);
  });
});
