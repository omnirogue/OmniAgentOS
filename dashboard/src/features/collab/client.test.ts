import { beforeEach, describe, expect, it, vi } from "vitest";

const { fetchWithTimeoutMock } = vi.hoisted(() => ({
  fetchWithTimeoutMock: vi.fn(),
}));

vi.mock("@/lib/fetchTimeout", () => ({
  fetchWithTimeout: fetchWithTimeoutMock,
}));

import { fetchWithTimeout } from "@/lib/fetchTimeout";
import { collabApi } from "./client";

describe("collabApi archive routes", () => {
  beforeEach(() => {
    vi.mocked(fetchWithTimeout).mockReset();
    vi.mocked(fetchWithTimeout).mockResolvedValue(
      new Response(JSON.stringify({ archived: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });

  it("keeps an arbitrary task id inside one encoded path segment", async () => {
    await collabApi.archiveTask("weird/id?x=1");

    expect(fetchWithTimeout).toHaveBeenCalledWith(
      "/api/board/weird%2Fid%3Fx%3D1/archive",
      expect.objectContaining({ method: "POST", body: "{}" }),
    );
  });
});

describe("collabApi sibling routes encode path segments too", () => {
  beforeEach(() => {
    vi.mocked(fetchWithTimeout).mockReset();
    vi.mocked(fetchWithTimeout).mockResolvedValue(
      new Response(JSON.stringify({}), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });

  const weirdId = "weird/id?x=1#frag";
  const weirdIdEncoded = "weird%2Fid%3Fx%3D1%23frag";

  it("keeps an arbitrary task id inside one encoded path segment for claimTask", async () => {
    await collabApi.claimTask(weirdId, "agt-1");

    expect(fetchWithTimeout).toHaveBeenCalledWith(
      `/api/collab/board/${weirdIdEncoded}/claim`,
      expect.objectContaining({ method: "POST", body: JSON.stringify({ agent_id: "agt-1" }) }),
    );
  });

  it("keeps an arbitrary channel id inside one encoded path segment for messages", async () => {
    await collabApi.messages(weirdId);

    expect(fetchWithTimeout).toHaveBeenCalledWith(
      `/api/collab/channels/${weirdIdEncoded}/messages`,
      expect.anything(),
    );
  });

  it("keeps an arbitrary channel id inside one encoded path segment for postMessage", async () => {
    await collabApi.postMessage(weirdId, "hello");

    expect(fetchWithTimeout).toHaveBeenCalledWith(
      `/api/collab/channels/${weirdIdEncoded}/messages`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ from_agent: "operator", body: "hello", kind: "message" }),
      }),
    );
  });
});
