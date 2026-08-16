import { describe, expect, it } from "vitest";
import { parseAlertsTail } from "../parsers/parseAlerts";

describe("parseAlertsTail", () => {
  it("keeps only '- ' bullet lines, dropping headers/blank lines", () => {
    const text = ["# ALERTS", "", "- 2026-08-14T05:50Z first alert", "not a bullet", "- 2026-08-14T06:01Z second alert"].join(
      "\n",
    );
    expect(parseAlertsTail(text)).toEqual([
      "- 2026-08-14T05:50Z first alert",
      "- 2026-08-14T06:01Z second alert",
    ]);
  });

  it("takes at most the last 5 bullets", () => {
    const text = Array.from({ length: 8 }, (_, i) => `- alert ${i}`).join("\n");
    const result = parseAlertsTail(text);
    expect(result).toHaveLength(5);
    expect(result[0]).toBe("- alert 3");
    expect(result[4]).toBe("- alert 7");
  });

  it("truncates a line over 300 chars with an ellipsis", () => {
    const long = `- ${"x".repeat(400)}`;
    const result = parseAlertsTail(long);
    expect(result).toHaveLength(1);
    expect(result[0].length).toBe(301); // 300 chars + ellipsis
    expect(result[0].endsWith("…")).toBe(true);
  });

  it("returns an empty array when there are no bullets", () => {
    expect(parseAlertsTail("nothing here")).toEqual([]);
  });
});
