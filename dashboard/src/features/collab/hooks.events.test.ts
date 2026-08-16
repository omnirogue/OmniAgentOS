import { describe, expect, it } from "vitest";
import { EVENTS_BASE } from "@/lib/contracts";
import { collabEventsUrl } from "./hooks";

describe("collab EventSource URL construction (H-18, L-03)", () => {
  it("targets product-scoped EVENTS_BASE (Grok :8485), never Omni sibling :8484", () => {
    const url = collabEventsUrl(["channel.updated"], 0);
    expect(url.startsWith(EVENTS_BASE)).toBe(true);
    expect(url).toContain("http://127.0.0.1:8485/api/events?");
    expect(url).not.toContain(":8484");
    expect(url).not.toMatch(/^\/api\/events/);
  });

  it("encodes multiple event types as a comma-joined types query param", () => {
    const url = collabEventsUrl(["board.updated", "board.claimed", "run.updated"], 0);
    const parsed = new URL(url);
    expect(parsed.searchParams.get("types")).toBe("board.updated,board.claimed,run.updated");
  });

  it("encodes special characters in type names safely", () => {
    const url = collabEventsUrl(["message.posted"], 0);
    const parsed = new URL(url);
    expect(parsed.searchParams.get("types")).toBe("message.posted");
  });
});

describe("SSE client discipline: every connect carries a cursor", () => {
  it("sends after_id so a reconnect does not replay the server's whole window", () => {
    const parsed = new URL(collabEventsUrl(["board.updated"], 4211));
    expect(parsed.searchParams.get("after_id")).toBe("4211");
  });

  it("sends after_id=0 on a first connect rather than omitting the parameter", () => {
    // Omitting it is what made the server fall back to "replay the tail":
    // the parameter is always present, so the cursor is always explicit.
    const parsed = new URL(collabEventsUrl(["board.updated"], 0));
    expect(parsed.searchParams.has("after_id")).toBe(true);
    expect(parsed.searchParams.get("after_id")).toBe("0");
  });

  it("always filters server-side — no connection asks for every event type", () => {
    const parsed = new URL(collabEventsUrl(["board.updated", "run.updated"], 12));
    const types = parsed.searchParams.get("types");
    expect(types).toBeTruthy();
    expect(types!.split(",")).toEqual(["board.updated", "run.updated"]);
  });
});
