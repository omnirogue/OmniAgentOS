/**
 * TypeScript mirror of the `GET /api/banking?day=YYYY-MM-DD` contract
 * (omniagentos/api/routes/banking.py). Verify field names against it before
 * changing anything here.
 *
 * This is a feature-local contracts file (features/cash/contracts.ts), NOT
 * src/lib/contracts.ts — that file is FROZEN and owned by the lead for the
 * shared run/task/approval surface. This one is scoped to the Cash page only,
 * following the same per-feature module pattern as features/revenue/contracts.ts.
 *
 * IMPORTANT (label accuracy — this is REAL money):
 *   • `totals.money_out_usd` is TRUE cash out — actual debits from the bank
 *     accounts — NOT accrued cost, COGS, or ad spend. Never relabel it.
 *   • `totals.cash_balance_usd` is the sum of latest-available balance
 *     snapshots. Always render `totals.note` verbatim so that caveat travels
 *     with the number.
 */

export const DATA_QUALITY_LEVELS = ["warn", "info"] as const;
export type DataQualityLevel = (typeof DATA_QUALITY_LEVELS)[number];

export interface CashTotals {
  /** Sum of latest-available balances across all accounts. */
  cash_balance_usd: number;
  /** Deposits (credits) that posted on the day. */
  money_in_usd: number;
  /** TRUE cash expenses — actual debits out of the bank accounts (incl. the
   * daily card autopay). */
  money_out_usd: number;
  /** money_in_usd − money_out_usd (may be negative). */
  net_flow_usd: number;
  /** Server-authored honesty caption — render verbatim as fine print under the
   * Money Out / cash tiles; never paraphrase it away. */
  note: string;
  /** Sum of the CREDIT sub-ledger's available balance across accounts. On a
   * setup that sweeps cash to auto-pay a charge card daily this is the REAL
   * spendable reserve — but it is a credit line, NOT owned cash. Never fold
   * this into `cash_balance_usd`; always render `available_credit_note`
   * alongside it. */
  available_credit_usd: number;
  /** Server-authored caption for `available_credit_usd` — render verbatim
   * wherever the figure is shown so it never reads as owned cash. */
  available_credit_note: string;
  /** Server-authored honesty note, present only when `cash_balance_usd` is
   * effectively $0 AND there is available credit to point to. Render it near
   * the Cash Balance KPI verbatim when non-null; `null` means it does not
   * apply for this day. */
  cash_sweep_note: string | null;
}

export interface CashMonthToDate {
  money_in_usd: number;
  money_out_usd: number;
  net_flow_usd: number;
}

export interface CashAccountRow {
  provider: string;
  brand: string;
  name: string;
  mask: string;
  kind: string;
  balance_usd: number;
  money_in_usd: number;
  money_out_usd: number;
}

export interface CashTransactionRow {
  account: string;
  posted_at: string | null;
  /** Signed: positive = deposit, negative = expense. */
  amount_usd: number;
  description: string;
}

export interface DataQualityNote {
  level: DataQualityLevel;
  message: string;
}

/** The honesty surface: which banks are actually feeding the numbers above, and
 * which are known but not wired yet. Must always be rendered. */
export interface CashCoverage {
  banks_live: string[];
  not_wired: string[];
}

export interface CashDay {
  /** YYYY-MM-DD, America/New_York calendar day. */
  day: string;
  timezone: string;
  /** ISO 8601 timestamp of when this snapshot was computed. */
  generated_at: string;
  totals: CashTotals;
  month_to_date: CashMonthToDate;
  by_account: CashAccountRow[];
  largest_transactions: CashTransactionRow[];
  data_quality: DataQualityNote[];
  coverage: CashCoverage;
}
