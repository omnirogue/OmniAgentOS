import { beforeEach, describe, expect, it, vi } from "vitest";

const { proxyAuthorizedMock, proxyPublicReadMock } = vi.hoisted(() => ({
  proxyAuthorizedMock: vi.fn(() =>
    Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 })),
  ),
  proxyPublicReadMock: vi.fn(() =>
    Promise.resolve(new Response(JSON.stringify({ tasks: [] }), { status: 200 })),
  ),
}));

vi.mock("@/lib/serverProxy", () => ({
  proxyAuthorized: proxyAuthorizedMock,
  proxyPublicRead: proxyPublicReadMock,
}));

import { GET, POST } from "./route";
import { tasksUpstreamPath } from "./upstreamPath";
import type { NextRequest } from "next/server";

function nextRequest(url: string, init?: RequestInit): NextRequest {
  const parsed = new URL(url);
  const request = new Request(url, init) as NextRequest;
  Object.defineProperty(request, "nextUrl", {
    value: parsed,
    configurable: true,
  });
  return request;
}

describe("Tasks API route ownership (C-09)", () => {
  beforeEach(() => {
    proxyAuthorizedMock.mockClear();
    proxyPublicReadMock.mockClear();
  });

  it("exports POST for voice-dispatch creation", () => {
    expect(typeof POST).toBe("function");
  });

  it("exports GET so an explicit Next handler does not silently 405 / shadow catch-all", () => {
    expect(typeof GET).toBe("function");
  });

  it("GET proxies via public read to backend /api/tasks", async () => {
    const request = nextRequest("http://dashboard.test/api/tasks");
    const response = await GET(request);
    expect(proxyPublicReadMock).toHaveBeenCalledWith("/api/tasks", request);
    expect(proxyAuthorizedMock).not.toHaveBeenCalled();
    expect(response.status).toBe(200);
  });

  it("GET forwards query string so project counts and Activity filters reach the backend", async () => {
    const request = nextRequest(
      "http://dashboard.test/api/tasks?project_id=proj-1&state=running&limit=50",
    );
    expect(tasksUpstreamPath(request, "GET")).toBe(
      "/api/tasks?project_id=proj-1&state=running&limit=50",
    );
    const response = await GET(request);
    expect(proxyPublicReadMock).toHaveBeenCalledWith(
      "/api/tasks?project_id=proj-1&state=running&limit=50",
      request,
    );
    expect(response.status).toBe(200);
  });

  it("GET with empty search still targets /api/tasks without a trailing ?", async () => {
    const request = nextRequest("http://dashboard.test/api/tasks");
    expect(tasksUpstreamPath(request, "GET")).toBe("/api/tasks");
  });

  it("POST does not append client query noise to the create path", async () => {
    const request = nextRequest("http://dashboard.test/api/tasks?noise=1", {
      method: "POST",
    });
    expect(tasksUpstreamPath(request, "POST")).toBe("/api/tasks");
    const response = await POST(request);
    expect(proxyAuthorizedMock).toHaveBeenCalledWith("/api/tasks", request, "POST");
    expect(response.status).toBe(200);
  });

  it("POST proxies to backend /api/tasks with POST method", async () => {
    const request = nextRequest("http://dashboard.test/api/tasks", { method: "POST" });
    const response = await POST(request);
    expect(proxyAuthorizedMock).toHaveBeenCalledWith("/api/tasks", request, "POST");
    expect(response.status).toBe(200);
  });
});
