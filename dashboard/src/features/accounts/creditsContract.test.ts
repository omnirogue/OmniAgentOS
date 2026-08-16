import { describe, expect, it } from "vitest";
import { formatCredits } from "./format";
import type { AccountCredits } from "./types";

/**
 * Contract parity: `decimal_places` is nullable ON THE WIRE.
 *
 * omniagentos/accounts/usage.py declares `decimal_places: int | None = None`,
 * and tests/accounts/test_usage.py
 * (`test_unknown_decimal_places_serializes_as_json_null_not_zero`) pins the
 * public dump: when the provider never reported a scale, the key is PRESENT
 * and JSON null — never 0, never omitted. A frontend type of plain `number`
 * was a lie about that wire shape; this fixture is the exact three-valued
 * payload the backend guarantees, and it must typecheck as AccountCredits.
 */
const UNKNOWN_SCALE_CREDITS: AccountCredits = {
  enabled: true,
  used: 1234,
  limit: 5000,
  used_amount: null,
  limit_amount: null,
  percent: null,
  currency: "USD",
  decimal_places: null,
  balance: null,
  unlimited: false,
  disabled_reason: null,
};

const KNOWN_SCALE_CREDITS: AccountCredits = {
  enabled: true,
  used: 1234,
  limit: 5000,
  used_amount: 12.34,
  limit_amount: 50.0,
  percent: 24.68,
  currency: "USD",
  decimal_places: 2,
  balance: null,
  unlimited: false,
  disabled_reason: null,
};

describe("credits contract parity with the backend", () => {
  it("accepts the pinned unknown-scale wire payload (decimal_places: null) and fabricates no amount", () => {
    // Unknown scale means the major-unit amounts are unknown too — the label
    // must be null (nothing to show), never a made-up dollar figure.
    expect(formatCredits(UNKNOWN_SCALE_CREDITS)).toBeNull();
  });

  it("still renders a known-scale payload as before (regression guard)", () => {
    // Mirror the implementation's locale handling instead of hardcoding a
    // locale-specific "$12.34" — the assertion is about the SHAPE surviving
    // the nullability change, not about any one locale's currency rendering.
    const money = (value: number) =>
      new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" }).format(value);
    expect(formatCredits(KNOWN_SCALE_CREDITS)).toBe(`${money(12.34)} of ${money(50)}`);
  });

  it("null decimal_places is assignable (compile-time pin of number | null)", () => {
    const scale: AccountCredits["decimal_places"] = null;
    expect(scale).toBeNull();
  });
});
