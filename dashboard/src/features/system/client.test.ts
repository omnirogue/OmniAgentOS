import { beforeEach, describe, expect, it, vi } from "vitest";
import { patchImproverPrompt, patchSystemAgent } from "./client";

const fetchMock = vi.fn();

describe("system mutation clients", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  it("does not report an agent edit as successful when the 2xx body is malformed", async () => {
    fetchMock.mockResolvedValue(new Response("{}", { status: 200 }));

    await expect(
      patchSystemAgent("agent/name", { model: "sonnet" }),
    ).rejects.toThrow("unparseable agent update response");
  });

  it("does not report a prompt edit as successful when the 2xx body is malformed", async () => {
    fetchMock.mockResolvedValue(new Response("{}", { status: 200 }));

    await expect(
      patchImproverPrompt("nightly/guard", "updated prompt"),
    ).rejects.toThrow("unparseable improver prompt response");
  });
});
