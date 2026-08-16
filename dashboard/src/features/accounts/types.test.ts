import { describe, expect, it } from "vitest";
import { formatCredits } from "./format";
import type { AccountCredits } from "./types";

/**
 * Dashboard contract parity — omniagentos/accounts/usage.py:115 made
 * `AccountCredits.decimal_places` `int | None`, with null pinned on the wire
 * for an unknown scale (tests/accounts/test_usage.py:481-513: "unknown must
 * never serialize as 0"). The TypeScript mirror previously declared
 * `decimal_places: number` — non-nullable — which lied about what the API
 * sends and would fail to compile against a real API row carrying `null`.
 *
 * No dashboard code reads `decimal_places` directly today (grepped the whole
 * dashboard/ tree): `used_amount` / `limit_amount` arrive pre-converted to
 * major units, so `formatCredits` never needs to divide by the scale. This
 * test proves the type accepts `null` (a TS compile failure here is the
 * red-first signal against the unfixed `number` type) and that a credits
 * object carrying it still formats safely — no `null`, `NaN`, or `undefined`
 * leaking into the rendered string.
 */
function baseCredits(overrides: Partial<AccountCredits> = {}): AccountCredits {
  return {
    enabled: true,
    used: 1234,
    limit: 5000,
    used_amount: 12.34,
    limit_amount: 50,
    percent: 24.68,
    currency: "USD",
    decimal_places: null,
    balance: null,
    unlimited: false,
    disabled_reason: null,
    ...overrides,
  };
}

describe("AccountCredits.decimal_places nullability", () => {
  it("type-checks with a null decimal_places (unknown scale)", () => {
    const credits: AccountCredits = baseCredits({ decimal_places: null });
    expect(credits.decimal_places).toBeNull();
  });

  it("formatCredits renders safely off used_amount/limit_amount when decimal_places is null", () => {
    const credits = baseCredits({ decimal_places: null, used_amount: 12.34, limit_amount: 50 });
    const label = formatCredits(credits);
    expect(label).not.toBeNull();
    expect(label).not.toMatch(/null|undefined|NaN/);
    expect(label).toBe("$12.34 of $50.00");
  });

  it("still type-checks and formats with a known decimal_places (regression guard)", () => {
    const credits = baseCredits({ decimal_places: 2 });
    expect(formatCredits(credits)).toBe("$12.34 of $50.00");
  });
});
