/** Synthetic, API-shaped fixture for the Cash page. Enable locally with
 * NEXT_PUBLIC_USE_CASH_FIXTURES=true (mirrors features/revenue/fixtures.ts's
 * NEXT_PUBLIC_USE_REVENUE_FIXTURES). Default OFF: production always talks to the
 * real GET /api/banking route. This lets the page render/lint/build end-to-end
 * against a realistic shape. */
import type { CashDay } from "./contracts";

export const USE_CASH_FIXTURES = process.env.NEXT_PUBLIC_USE_CASH_FIXTURES === "true";

/**
 * A deliberately honest day, illustrating a Slash-style banking setup:
 * incoming cash sweeps daily to auto-pay a charge card, so the cash balance
 * is genuinely ~$0 while the spendable reserve is available credit. Teller
 * (the user's linked checking/savings) is NOT wired — exactly the gap the
 * "Data quality & coverage" panel exists to surface rather than paper over.
 */
export const FIXTURE_CASH_DAY: CashDay = {
  day: "2026-07-19",
  timezone: "America/New_York",
  generated_at: "2026-07-20T06:03:00Z",
  totals: {
    cash_balance_usd: 0.0,
    money_in_usd: 4200.0,
    money_out_usd: 900.0,
    net_flow_usd: 3300.0,
    note: "Money out = actual debits from your bank accounts, incl. the daily card autopay (your true cash expenses); balances are latest available.",
    available_credit_usd: 5000.0,
    available_credit_note: "Available credit is your charge-card credit line (spendable reserve), not owned cash.",
    cash_sweep_note:
      "Cash sweeps daily to auto-pay your charge card — your spendable reserve is available credit, not cash.",
  },
  month_to_date: {
    money_in_usd: 21000.0,
    money_out_usd: 23500.0,
    net_flow_usd: -2500.0,
  },
  by_account: [
    {
      provider: "slash",
      brand: "AcmeUni",
      name: "AcmeUni Operating",
      mask: "1111",
      kind: "checking",
      balance_usd: 0.0,
      money_in_usd: 4200.0,
      money_out_usd: 900.0,
    },
    {
      provider: "slash",
      brand: "Initech",
      name: "Initech Card",
      mask: "2222",
      kind: "card",
      balance_usd: 0.0,
      money_in_usd: 0,
      money_out_usd: 0,
    },
  ],
  largest_transactions: [
    { account: "AcmeUni Operating ••1111", posted_at: "2026-07-19T09:15:00Z", amount_usd: 3500.0, description: "Client wire transfer" },
    { account: "AcmeUni Operating ••1111", posted_at: "2026-07-19T14:12:00Z", amount_usd: 1200.0, description: "Stripe ACH payout" },
    { account: "AcmeUni Operating ••1111", posted_at: "2026-07-19T23:55:00Z", amount_usd: -800.0, description: "Daily Credit Card Payment (autopay sweep)" },
    { account: "AcmeUni Operating ••1111", posted_at: "2026-07-19T23:56:00Z", amount_usd: -25.0, description: "Slash monthly fee" },
  ],
  data_quality: [
    {
      level: "warn",
      message:
        "Teller (linked checking/savings) is NOT included: it needs mutual-TLS client-cert auth the broker does not yet support. The figures above are Slash-only until that is wired.",
    },
    {
      level: "info",
      message:
        "A card account's balance is its OUTSTANDING balance (money owed), not spendable cash — it is summed into Total Cash Balance; read that total with that in mind.",
    },
  ],
  coverage: {
    banks_live: ["slash (AcmeUni)", "slash (Initech)"],
    not_wired: ["Teller (linked banks — broker mTLS/client-cert auth not wired)", "Mercury", "Brex", "Relay", "Wise"],
  },
};
