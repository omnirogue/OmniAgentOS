import { describe, expect, it } from "vitest";
import type { CompaniesPayload } from "./contracts";
import { buildCompaniesPortfolio, formatCollectedRoas } from "./logic";

const payload: CompaniesPayload = {
  companies: [
    { id: "co_acmeuni", slug: "AcmeUni", name: "AcmeUni", products: [] },
    { id: "co_omni", slug: "omniagentos", name: "OmniAgentOS", products: [] },
    { id: "co_personal", slug: "personal", name: "Personal", products: [] },
  ],
  goals: [{ id: "goal_acmeuni", org_company_id: "co_acmeuni", title: "Grow", horizon: "long_term", status: "active" }],
  revenue: {
    days: 7,
    generated_at: "2026-08-14T12:00:00Z",
    verticals: [
      { vertical: "acmeuni", revenue_usd: 125, ad_spend_usd: 0, roas_collected: null, refunds_usd: 0, days_with_data: 2 },
      { vertical: "global", revenue_usd: 25, ad_spend_usd: 0, roas_collected: null, refunds_usd: 0, days_with_data: 1 },
    ],
    sources: [{ key: "AcmeUni:stripe", status: "failed", last_ok_at: null, last_day: null, consecutive_failures: 3 }],
  },
};

describe("companies portfolio projection", () => {
  it("matches vertical and collector source keys case-insensitively while hiding personal", () => {
    const portfolio = buildCompaniesPortfolio(payload);
    expect(portfolio.brands).toHaveLength(1);
    expect(portfolio.brands[0]?.company.slug).toBe("AcmeUni");
    expect(portfolio.brands[0]?.vertical?.revenue_usd).toBe(125);
    expect(portfolio.brands[0]?.sources[0]?.key).toBe("AcmeUni:stripe");
    expect(portfolio.brands.some((view) => view.company.slug === "personal")).toBe(false);
  });

  it("renders null ROAS as an em dash", () => {
    expect(formatCollectedRoas(null)).toBe("—");
  });

  it("retains a global vertical as a small unattributed revenue surface", () => {
    expect(buildCompaniesPortfolio(payload).unattributedGlobal).toMatchObject({ vertical: "global", revenue_usd: 25 });
  });
});
