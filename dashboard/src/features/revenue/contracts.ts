/**
 * TypeScript mirror of the FROZEN `GET /api/revenue?day=YYYY-MM-DD` contract
 * (see .fusion/REVENUE-DASH.md). The backend owns and builds this route in
 * parallel — verify field names against it before changing anything here.
 *
 * This is a feature-local contracts file (features/revenue/contracts.ts),
 * NOT src/lib/contracts.ts — that file is FROZEN and owned by the lead for
 * the shared run/task/approval surface; this one is scoped to the Revenue
 * page only, following the same per-feature module pattern as
 * features/steward/types.ts, features/galaxy/types.ts, etc.
 *
 * IMPORTANT (label accuracy): `totals.gross_contribution_usd` is
 * revenue − ad spend. It is NOT net profit — it excludes COGS, refunds,
 * payroll, infra, and every other cost. Never rename/relabel it "profit"
 * anywhere in this feature.
 */

export const DATA_QUALITY_LEVELS = ["warn", "info"] as const;
export type DataQualityLevel = (typeof DATA_QUALITY_LEVELS)[number];

export interface RevenueTotals {
  revenue_usd: number;
  ad_spend_usd: number;
  /** revenue_usd − ad_spend_usd. NOT net profit — see module doc comment. */
  gross_contribution_usd: number;
  /** Blended real revenue ÷ ad spend (revenue_usd / ad_spend_usd) — NOT the
   * Meta attribution `roas` field. Null when ad_spend_usd is 0 (never a
   * fabricated 0); render as "—". */
  blended_roas: number | null;
  /** Return on ad spend as a ratio: (revenue_usd − ad_spend_usd) / ad_spend_usd
   * (0.5 == +50%). Null when ad_spend_usd is 0; render as "—". */
  roi: number | null;
  payment_failures: number;
  /** Server-authored honesty caption — always render verbatim as fine print
   * under the Gross Contribution tile; never paraphrase it away. */
  note: string;
}

export interface RevenueVerticalRow {
  vertical: string;
  revenue_usd: number;
  ad_spend_usd: number;
  gross_contribution_usd: number;
  /** Return on ad spend. Null/non-finite (e.g. zero spend) is normalized to
   * null — render as "—", never as Infinity or NaN. */
  roas: number | null;
  /** Blended real revenue ÷ ad spend for this vertical — NOT the Meta
   * attribution `roas` above. Null when ad_spend_usd is 0; render as "—". */
  blended_roas: number | null;
  /** Return on ad spend as a ratio for this vertical (0.5 == +50%). Null
   * when ad_spend_usd is 0; render as "—". */
  roi: number | null;
  payment_failures: number;
}

export interface RevenueSourceRow {
  vertical: string;
  source: string;
  revenue_usd: number;
  ad_spend_usd: number;
  payment_failures: number;
}

export interface RevenueCampaignRow {
  vertical: string;
  source: string;
  campaign_id: string;
  campaign_name: string;
  ad_spend_usd: number;
  roas: number | null;
  revenue_attributed_usd: number;
}

export interface DataQualityNote {
  level: DataQualityLevel;
  message: string;
}

export interface SourceStatus {
  vertical: string;
  source: string;
  status: "ok" | "failed" | "unconfigured";
  message: string;
  consecutive_failures: number;
}

/** The honesty surface: what the numbers above actually include vs. don't.
 * Must always be rendered — this is what lets the user trust (or
 * appropriately distrust) the KPI tiles above. */
export interface RevenueCoverage {
  revenue_sources_live: string[];
  spend_sources_live: string[];
  not_wired: string[];
}

export interface RevenueDay {
  /** YYYY-MM-DD, America/New_York calendar day. */
  day: string;
  timezone: string;
  /** ISO 8601 timestamp of when this snapshot was computed. */
  generated_at: string;
  totals: RevenueTotals;
  by_vertical: RevenueVerticalRow[];
  by_source: RevenueSourceRow[];
  by_campaign: RevenueCampaignRow[];
  data_quality: DataQualityNote[];
  source_status: SourceStatus[];
  coverage: RevenueCoverage;
}
