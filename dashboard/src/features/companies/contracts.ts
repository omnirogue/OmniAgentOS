/** Mirrors the three read-only endpoints used exclusively by /companies. */

export type GoalHorizon = "long_term" | "short_term";
export type GoalStatus = "active" | "paused" | "achieved" | "archived";
export type CollectorStatus = "ok" | "failed" | "unconfigured";

export interface CompanyProduct {
  id: string;
  company_id: string;
  slug: string;
  name: string;
}

export interface Company {
  id: string;
  slug: string;
  name: string;
  products: CompanyProduct[];
}

export interface CompanyGoal {
  id: string;
  org_company_id: string;
  title: string;
  horizon: GoalHorizon;
  status: GoalStatus;
}

export interface RevenueVertical {
  vertical: string;
  /** null = ABSENT from the wire — never coerced to 0 (honesty rule). */
  revenue_usd: number | null;
  ad_spend_usd: number | null;
  /** Collected-revenue numerator (roas_collected on the wire), never
   *  ad-platform-attributed value. null when spend is 0 or absent. */
  roas_collected: number | null;
  refunds_usd: number | null;
  days_with_data: number | null;
}

export interface RevenueSource {
  /** Backend status key: `<vertical>:<source-family>` (families only —
   *  account ids are coarsened server-side). */
  key: string;
  /** Known values: CollectorStatus | "never_collected"; any OTHER string is
   *  rendered explicitly amber, never dropped — keep this open. */
  status: CollectorStatus | "never_collected" | (string & {});
  last_ok_at: string | null;
  last_day: string | null;
  consecutive_failures: number;
}

export interface CompaniesPayload {
  companies: Company[];
  goals: CompanyGoal[];
  revenue: {
    days: number;
    generated_at: string;
    verticals: RevenueVertical[];
    sources: RevenueSource[];
  };
}

export interface CompanyView {
  company: Company;
  goals: CompanyGoal[];
  vertical: RevenueVertical | null;
  sources: RevenueSource[];
}

export interface CompaniesPortfolio {
  brands: CompanyView[];
  platform: CompanyView | null;
  unattributedGlobal: RevenueVertical | null;
  generated_at: string;
}
