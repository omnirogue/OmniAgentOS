import { describe, expect, it } from "vitest";
import { parseGateTail } from "../parsers/parseGate";

const FIXTURE = [
  "  train/gl-39b9b8a833e9 @ acb5c03645e2 still gating (waiting)",
  "  train/gl-58125c4118d4 @ 02954f3996a9 still gating (waiting)",
  "  train/gl-3624ff00f8ec @ 134cb0c173cf still gating (waiting)",
  "  gate slots full (3/3) — train/gl-66ebb16fa7d8 deferred to next tick",
  "  gate slots full (3/3) — train/gl-a9101c1bb364 deferred to next tick",
  '{',
  '  "at": "2026-08-14T08:35:35Z",',
  '  "outcomes": []',
  '}',
  '{',
  '  "at": "2026-08-14T08:40:04Z",',
  '  "outcomes": []',
  '}',
  "  no trains assembled",
].join("\n");

describe("parseGateTail", () => {
  it("extracts up to the last 5 matching lines, trimmed", () => {
    const result = parseGateTail(FIXTURE);
    expect(result.trainLines).toEqual([
      "train/gl-58125c4118d4 @ 02954f3996a9 still gating (waiting)",
      "train/gl-3624ff00f8ec @ 134cb0c173cf still gating (waiting)",
      "gate slots full (3/3) — train/gl-66ebb16fa7d8 deferred to next tick",
      "gate slots full (3/3) — train/gl-a9101c1bb364 deferred to next tick",
      "no trains assembled",
    ]);
  });

  it("takes the LAST \"at\" stamp in the tail", () => {
    expect(parseGateTail(FIXTURE).lastAt).toBe("2026-08-14T08:40:04Z");
  });

  it("caps at 5 lines even with more matches", () => {
    const many = Array.from({ length: 8 }, (_, i) => `  gate slots full (${i}/3)`).join("\n");
    expect(parseGateTail(many).trainLines).toHaveLength(5);
  });

  it("returns empty trainLines and null lastAt when nothing matches", () => {
    expect(parseGateTail("nothing to see here")).toEqual({ trainLines: [], lastAt: null });
  });
});
