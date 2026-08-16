/**
 * The chat ctx-row reads GET /api/workfs/tree, which FastAPI gates with the
 * session token. The dashboard's catch-all proxy decides per-path whether to
 * attach that token — and "workfs" was missing from the allowlist, so the
 * Work-folder tree was 401-dead through the proxy.
 *
 * This test pins the one line that fixes it (and pins that an unrelated path
 * still takes the public relay, so the allowlist stays a list, not a bypass).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import type { NextRequest } from "next/server";

const { proxyRead, proxyPublicRead, proxyAuthorized } = vi.hoisted(() => {
  const stub = () => vi.fn((path: string) => Promise.resolve(new Response(path)));
  return { proxyRead: stub(), proxyPublicRead: stub(), proxyAuthorized: stub() };
});

vi.mock("@/lib/serverProxy", () => ({ proxyRead, proxyPublicRead, proxyAuthorized }));

// NOTE: the vi.mock above is hoisted, so this import still gets the stubs.
import { GET } from "@/app/api/[...path]/route";

/** The handler only reads `nextUrl.search` off the request. */
const request = { nextUrl: { search: "" } } as unknown as NextRequest;

describe("api proxy read allowlist", () => {
  beforeEach(() => {
    proxyRead.mockClear();
    proxyPublicRead.mockClear();
  });

  it("sends GET /api/workfs/tree through the token-attaching read proxy", async () => {
    await GET(request, { params: Promise.resolve({ path: ["workfs", "tree"] }) });

    expect(proxyRead).toHaveBeenCalledTimes(1);
    expect(proxyRead.mock.calls[0]![0]).toBe("/api/workfs/tree");
    expect(proxyPublicRead).not.toHaveBeenCalled();
  });

  it("still relays a non-allowlisted read publicly", async () => {
    await GET(request, { params: Promise.resolve({ path: ["health"] }) });

    expect(proxyPublicRead).toHaveBeenCalledTimes(1);
    expect(proxyRead).not.toHaveBeenCalled();
  });
});
