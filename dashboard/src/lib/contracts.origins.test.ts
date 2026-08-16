import { describe, expect, it } from "vitest";
import { API_BASE, EVENTS_BASE } from "./contracts";

describe("product-scoped origin constants (H-18, L-03)", () => {
  it("uses same-origin empty API_BASE for browser proxy reads/writes", () => {
    expect(API_BASE).toBe("");
  });

  it("points SSE EVENTS_BASE at Grok :8485, not Omni sibling :8484", () => {
    expect(EVENTS_BASE).toBe("http://127.0.0.1:8485");
    expect(EVENTS_BASE).not.toContain(":8484");
  });
});
