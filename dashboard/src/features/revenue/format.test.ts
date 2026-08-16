import { describe, expect, it } from "vitest";
import { formatMoney, formatRoas, formatRoiPercent } from "./format";

describe("Revenue blended-return formatters", () => {
  // formatRoas is shared by the attribution `roas` field and the new
  // `blended_roas` field — both are "revenue / spend" multipliers.
  it("formats blended ROAS as an N.NNx multiplier", () => {
    expect(formatRoas(2.3333)).toBe("2.33x");
    expect(formatRoas(0)).toBe("0.00x");
  });

  it("renders blended ROAS as an em dash when there is no ad spend", () => {
    expect(formatRoas(null)).toBe("—");
    expect(formatRoas(undefined)).toBe("—");
  });

  it("formats a positive ROI ratio as a signed whole-percent", () => {
    // 0.5 == +50% per the report.py contract.
    expect(formatRoiPercent(0.5)).toBe("+50%");
    expect(formatRoiPercent(1.3333)).toBe("+133%");
  });

  it("formats a negative ROI ratio without a double sign", () => {
    expect(formatRoiPercent(-0.2)).toBe("-20%");
  });

  it("formats a zero ROI ratio without a leading plus", () => {
    expect(formatRoiPercent(0)).toBe("0%");
  });

  it("renders ROI as an em dash when there is no ad spend to divide by", () => {
    expect(formatRoiPercent(null)).toBe("—");
    expect(formatRoiPercent(undefined)).toBe("—");
    expect(formatRoiPercent(NaN)).toBe("—");
  });

  // Sanity check: unrelated existing formatter is untouched by this change.
  it("still formats money as before", () => {
    expect(formatMoney(456.78)).toBe("$456.78");
    expect(formatMoney(null)).toBe("—");
  });
});
