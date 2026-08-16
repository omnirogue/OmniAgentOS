import { Badge, Table, type TableColumn } from "@/design";
import { formatMoney } from "../format";
import styles from "../cash.module.css";
import type { CashAccountRow } from "../contracts";

export type BalancesTableProps = { rows: CashAccountRow[] };

function accountLabel(r: CashAccountRow): string {
  return r.mask ? `${r.name} ••${r.mask}` : r.name;
}

export function BalancesTable({ rows }: BalancesTableProps) {
  const columns: TableColumn<CashAccountRow>[] = [
    { key: "name", header: "Account", render: (r) => accountLabel(r) },
    {
      key: "brand",
      header: "Brand",
      render: (r) => (r.brand ? <Badge tone="neutral">{r.brand}</Badge> : <span className={styles.faint}>—</span>),
    },
    { key: "provider", header: "Provider", render: (r) => r.provider },
    {
      key: "kind",
      header: "Type",
      render: (r) => (r.kind ? r.kind : <span className={styles.faint}>—</span>),
    },
    {
      key: "balance_usd",
      header: "Balance",
      align: "right",
      sortable: true,
      sortValue: (r) => r.balance_usd,
      render: (r) => <span className={styles.moneyCell}>{formatMoney(r.balance_usd)}</span>,
    },
    {
      key: "money_in_usd",
      header: "Money In",
      align: "right",
      sortable: true,
      sortValue: (r) => r.money_in_usd,
      render: (r) => <span className={styles.moneyCell}>{formatMoney(r.money_in_usd)}</span>,
    },
    {
      key: "money_out_usd",
      header: "True Expenses",
      align: "right",
      sortable: true,
      sortValue: (r) => r.money_out_usd,
      render: (r) => <span className={styles.moneyCell}>{formatMoney(r.money_out_usd)}</span>,
    },
  ];

  return (
    <Table
      columns={columns}
      rows={rows}
      rowKey={(r) => `${r.provider}:${r.name}:${r.mask}`}
      emptyMessage="No bank accounts reported balances for this day."
      caption="Balance, money in and true expenses by account"
    />
  );
}
