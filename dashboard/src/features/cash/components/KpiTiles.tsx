import { cx, Stat } from "@/design";
import { formatMoney } from "../format";
import styles from "../cash.module.css";
import type { CashTotals } from "../contracts";

export type KpiTilesProps = { totals: CashTotals };

/**
 * The headline tiles. "True Expenses (Money Out)" is deliberately labeled as
 * actual debits — never "costs" or "spend" — with the server's `totals.note`
 * rendered verbatim as fine print so the honesty caption travels with the
 * number. "Available credit (spendable reserve)" sits right next to Total Cash
 * Balance: on this operator's setup, cash sweeps daily to auto-pay a charge
 * card, so cash balance alone understates what's actually spendable — but
 * available credit is a credit line, NOT owned cash, so its own note
 * (`available_credit_note`) is rendered verbatim too, never dropped. When
 * `cash_sweep_note` is present (cash balance ~$0), it renders directly under
 * the tiles as a short, honest explanation of why.
 */
export function KpiTiles({ totals }: KpiTilesProps) {
  return (
    <div>
      <div className={styles.statGrid}>
        <Stat
          label="Total Cash Balance"
          value={formatMoney(totals.cash_balance_usd)}
          tone="accent"
          hint="Sum of latest-available balances across all accounts."
        />
        <Stat
          label="Available credit (spendable reserve)"
          value={formatMoney(totals.available_credit_usd)}
          tone="default"
          hint={totals.available_credit_note}
        />
        <Stat label="Money In (deposits)" value={formatMoney(totals.money_in_usd)} tone="ok" />
        <Stat
          label="True Expenses (Money Out)"
          value={formatMoney(totals.money_out_usd)}
          tone="warn"
          hint={totals.note}
        />
        <Stat
          label="Net Flow"
          value={formatMoney(totals.net_flow_usd)}
          tone={totals.net_flow_usd >= 0 ? "ok" : "danger"}
        />
      </div>
      {totals.cash_sweep_note ? (
        <p className={cx(styles.qualityNote, styles.qualityNoteInfo, styles.cashSweepNote)}>
          {totals.cash_sweep_note}
        </p>
      ) : null}
    </div>
  );
}
