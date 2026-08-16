import { Card, cx, EmptyState, Table, type TableColumn } from "@/design";
import { formatPostedAt, formatSignedMoney } from "../format";
import styles from "../cash.module.css";
import type { CashTransactionRow } from "../contracts";

export type TransactionsTableProps = { rows: CashTransactionRow[] };

/** Largest transactions of the day by absolute amount, deposits and expenses
 * together. Amount is signed (+ deposit / − expense) so direction is legible. */
export function TransactionsTable({ rows }: TransactionsTableProps) {
  if (rows.length === 0) {
    return (
      <Card>
        <EmptyState title="No transactions" message="No bank transactions were recorded for this day." />
      </Card>
    );
  }

  const columns: TableColumn<CashTransactionRow>[] = [
    { key: "description", header: "Description", render: (r) => r.description || "—" },
    { key: "account", header: "Account", render: (r) => r.account },
    {
      key: "posted_at",
      header: "Posted",
      render: (r) => <span className={styles.faint}>{formatPostedAt(r.posted_at)}</span>,
    },
    {
      key: "amount_usd",
      header: "Amount",
      align: "right",
      sortable: true,
      sortValue: (r) => r.amount_usd,
      render: (r) => (
        <span className={cx(styles.moneyCell, r.amount_usd >= 0 ? styles.moneyIn : styles.moneyOut)}>
          {formatSignedMoney(r.amount_usd)}
        </span>
      ),
    },
  ];

  // Already largest-first from the server; keep amount sortable so the operator
  // can flip it or sort another column.
  return (
    <Table
      columns={columns}
      rows={rows}
      rowKey={(r) => `${r.account}:${r.posted_at}:${r.amount_usd}:${r.description}`}
      emptyMessage="No bank transactions were recorded for this day."
      caption="Largest transactions of the day by absolute amount"
    />
  );
}
