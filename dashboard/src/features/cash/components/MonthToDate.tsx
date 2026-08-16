import { formatMoney } from "../format";
import styles from "../cash.module.css";
import type { CashMonthToDate } from "../contracts";

export type MonthToDateProps = { mtd: CashMonthToDate };

/** Month-to-date flow roll-up. Balances are deliberately absent — a running sum
 * of daily balance snapshots is meaningless; only the flows accumulate. */
export function MonthToDate({ mtd }: MonthToDateProps) {
  return (
    <div className={styles.mtdRow}>
      <span className={styles.faint}>Month to date:</span>
      <span className={styles.mtdItem}>
        Money in <span className={styles.mtdValue}>{formatMoney(mtd.money_in_usd)}</span>
      </span>
      <span className={styles.mtdItem}>
        True expenses <span className={styles.mtdValue}>{formatMoney(mtd.money_out_usd)}</span>
      </span>
      <span className={styles.mtdItem}>
        Net flow <span className={styles.mtdValue}>{formatMoney(mtd.net_flow_usd)}</span>
      </span>
    </div>
  );
}
