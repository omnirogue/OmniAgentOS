import { describe, expect, it } from "vitest";
import { lastNonEmptyLine, parseRecyclerLine } from "../parsers/parseRecycler";

const JSONL_TAIL = [
  '{"action": "none", "reason": "turn young (4471s < 5400)", "child_age_s": 4471, "gate": false}',
  '{"action": "none", "reason": "turn young (4591s < 5400)", "child_age_s": 4591, "gate": false}',
  '{"action": "RECYCLED", "reason": "child_age_s 6200 >= 5400", "child_age_s": 6200, "gate": true}',
  "",
].join("\n");

describe("lastNonEmptyLine", () => {
  it("returns the last non-blank line of a JSONL tail", () => {
    expect(lastNonEmptyLine(JSONL_TAIL)).toBe(
      '{"action": "RECYCLED", "reason": "child_age_s 6200 >= 5400", "child_age_s": 6200, "gate": true}',
    );
  });

  it("returns null for an empty/whitespace-only tail", () => {
    expect(lastNonEmptyLine("")).toBeNull();
    expect(lastNonEmptyLine("\n\n   \n")).toBeNull();
  });
});

describe("parseRecyclerLine", () => {
  it("parses a well-formed JSON line", () => {
    const line = '{"action": "FORCE-RECYCLED", "reason": "unresponsive"}';
    expect(parseRecyclerLine(line)).toEqual({ action: "FORCE-RECYCLED", reason: "unresponsive" });
  });

  it("falls back to { raw } on malformed JSON (e.g. a tail cut mid-write)", () => {
    const truncated = '{"action": "none", "reason": "turn young (44';
    expect(parseRecyclerLine(truncated)).toEqual({ raw: truncated });
  });

  it("falls back to { raw } on a JSON array or scalar (not an object)", () => {
    expect(parseRecyclerLine("[1,2,3]")).toEqual({ raw: "[1,2,3]" });
    expect(parseRecyclerLine("42")).toEqual({ raw: "42" });
  });

  it("returns { raw: \"\" } when there was no line at all", () => {
    expect(parseRecyclerLine(null)).toEqual({ raw: "" });
  });
});
