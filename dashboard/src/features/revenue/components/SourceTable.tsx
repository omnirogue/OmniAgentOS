import { Badge, Table, type TableColumn } from "@/design";
import { formatMoney } from "../format";
import styles from "../revenue.module.css";
import type { RevenueSourceRow } from "../contracts";

export type SourceTableProps = { rows: RevenueSourceRow[] };

export function SourceTable({ rows }: SourceTableProps) {
  const columns: TableColumn<RevenueSourceRow>[] = [
    { key: "vertical", header: "Vertical", render: (r) => r.vertical },
    { key: "source", header: "Source", render: (r) => r.source },
    {
      key: "revenue_usd",
      header: "Revenue",
      align: "right",
      sortable: true,
      sortValue: (r) => r.revenue_usd,
      render: (r) => <span className={styles.moneyCell}>{formatMoney(r.revenue_usd)}</span>,
    },
    {
      key: "ad_spend_usd",
      header: "Ad Spend",
      align: "right",
      sortable: true,
      sortValue: (r) => r.ad_spend_usd,
      render: (r) => <span className={styles.moneyCell}>{formatMoney(r.ad_spend_usd)}</span>,
    },
    {
      key: "payment_failures",
      header: "Failures",
      align: "right",
      sortable: true,
      sortValue: (r) => r.payment_failures,
      render: (r) => (
        <Badge tone={r.payment_failures > 0 ? "warn" : "neutral"}>{r.payment_failures}</Badge>
      ),
    },
  ];

  return (
    <Table
      columns={columns}
      rows={rows}
      rowKey={(r) => `${r.vertical}:${r.source}`}
      emptyMessage="No revenue recorded by source for this day."
      caption="Revenue, ad spend and payment failures by vertical and source"
    />
  );
}
