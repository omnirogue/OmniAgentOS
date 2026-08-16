/** Synthetic, API-shaped fixture for the Revenue page. Enable locally with
 * NEXT_PUBLIC_USE_REVENUE_FIXTURES=true (mirrors features/steward/fixtures.ts's
 * NEXT_PUBLIC_USE_STEWARD_FIXTURES). Default OFF: production always talks to
 * the real GET /api/revenue route. This lets the page render/lint/build
 * end-to-end before the backend route exists. */
import type { RevenueDay } from "./contracts";

export const USE_REVENUE_FIXTURES = process.env.NEXT_PUBLIC_USE_REVENUE_FIXTURES === "true";

/**
 * A deliberately messy, illustrative day: AcmeUni's Stripe revenue is healthy,
 * but the Meta *account-level* spend total synced as $0 (a connector problem,
 * not genuinely zero spend) while the *campaign-level* pull still has
 * numbers from a periodic sync — exactly the kind of mismatch this
 * page's "Data quality & coverage" panel exists to surface, not hide.
 */
export const FIXTURE_REVENUE_DAY: RevenueDay = {
  day: "2026-07-19",
  timezone: "America/New_York",
  generated_at: "2026-07-20T11:03:00Z",
  totals: {
    revenue_usd: 620.15,
    ad_spend_usd: 0,
    gross_contribution_usd: 620.15,
    blended_roas: null,
    roi: null,
    payment_failures: 1,
    note:
      "Gross contribution = revenue − ad spend. This is NOT net profit — it excludes COGS, refunds, payroll, infrastructure, and every other cost.",
  },
  by_vertical: [
    {
      vertical: "AcmeUni",
      revenue_usd: 590.0,
      ad_spend_usd: 0,
      gross_contribution_usd: 590.0,
      roas: null,
      blended_roas: null,
      roi: null,
      payment_failures: 1,
    },
    {
      vertical: "Initech",
      revenue_usd: 30.15,
      ad_spend_usd: 0,
      gross_contribution_usd: 30.15,
      roas: null,
      blended_roas: null,
      roi: null,
      payment_failures: 0,
    },
  ],
  by_source: [
    { vertical: "AcmeUni", source: "stripe", revenue_usd: 590.0, ad_spend_usd: 0, payment_failures: 1 },
    { vertical: "AcmeUni", source: "meta", revenue_usd: 0, ad_spend_usd: 0, payment_failures: 0 },
    { vertical: "Initech", source: "stripe", revenue_usd: 30.15, ad_spend_usd: 0, payment_failures: 0 },
  ],
  by_campaign: [
    {
      vertical: "AcmeUni",
      source: "meta",
      campaign_id: "campaign_example_1",
      campaign_name: "Retarget-Campaign-A",
      ad_spend_usd: 40.0,
      roas: 1.8,
      revenue_attributed_usd: 72.0,
    },
    {
      vertical: "AcmeUni",
      source: "meta",
      campaign_id: "campaign_example_2",
      campaign_name: "Cold-Audience-B",
      ad_spend_usd: 25.0,
      roas: 0.9,
      revenue_attributed_usd: 22.5,
    },
    {
      vertical: "AcmeUni",
      source: "meta",
      campaign_id: "campaign_example_3",
      campaign_name: "Broad-Prospecting",
      ad_spend_usd: 0,
      roas: 0,
      revenue_attributed_usd: 0,
    },
  ],
  data_quality: [
    {
      level: "warn",
      message:
        "Meta account-level ad spend synced as $0.00 for 2026-07-19 — the campaign figures below are from a periodic sync and have not reconciled against the daily total yet. Treat ad_spend_usd totals as understated until the next full sync.",
    },
    {
      level: "warn",
      message: "1 Stripe payment failure recorded for AcmeUni on 2026-07-19 — see By Vertical below.",
    },
    {
      level: "info",
      message: "Initech has no ad spend source connected yet, so its gross contribution equals its revenue.",
    },
  ],
  source_status: [
    {
      vertical: "AcmeUni",
      source: "meta",
      status: "failed",
      message: "Meta account-level daily sync failed; campaign figures are from a periodic sync.",
      consecutive_failures: 1,
    },
  ],
  coverage: {
    revenue_sources_live: ["stripe"],
    spend_sources_live: ["meta"],
    not_wired: ["google_ads", "tiktok_ads", "cogs_ledger", "refunds_reconciliation"],
  },
};
