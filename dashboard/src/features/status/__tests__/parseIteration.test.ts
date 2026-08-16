import { describe, expect, it } from "vitest";
import { parseLastIterationEnd } from "../parsers/parseIteration";

const FIXTURE = [
  "───── 2026-08-14T06:21:53Z implementer iteration end rc=0",
  "───── 2026-08-14T06:22:53Z implementer iteration start",
  "some prose line with rc=1 in it that must not match",
  "───── 2026-08-14T07:17:08Z implementer iteration end rc=0",
  "───── 2026-08-14T07:18:24Z implementer iteration start",
].join("\n");

describe("parseLastIterationEnd", () => {
  it("returns the LAST iteration-end match, not the first", () => {
    expect(parseLastIterationEnd(FIXTURE, "implementer")).toEqual({
      lastIterEnd: "2026-08-14T07:17:08Z",
      lastRc: 0,
    });
  });

  it("returns nulls when no iteration-end line is present for the role", () => {
    expect(parseLastIterationEnd("nothing relevant here", "implementer")).toEqual({
      lastIterEnd: null,
      lastRc: null,
    });
  });

  it("does not cross-match a different role's iteration-end line", () => {
    const text = "───── 2026-08-14T08:22:06Z reviewer iteration end rc=0";
    expect(parseLastIterationEnd(text, "implementer")).toEqual({
      lastIterEnd: null,
      lastRc: null,
    });
  });

  it("parses a non-zero rc", () => {
    const text = "───── 2026-08-14T08:36:41Z planning iteration end rc=137";
    expect(parseLastIterationEnd(text, "planning")).toEqual({
      lastIterEnd: "2026-08-14T08:36:41Z",
      lastRc: 137,
    });
  });
});
