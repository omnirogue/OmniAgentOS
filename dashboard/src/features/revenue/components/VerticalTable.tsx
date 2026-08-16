import { Badge, Table, type TableColumn } from "@/design";
import { formatMoney, formatRoas, formatRoiPercent } from "../format";
import styles from "../revenue.module.css";
import type { RevenueVerticalRow } from "../contracts";

export type VerticalTableProps = { rows: RevenueVerticalRow[] };

export function VerticalTable({ rows }: VerticalTableProps) {
  const columns: TableColumn<RevenueVerticalRow>[] = [
    { key: "vertical", header: "Vertical", render: (r) => r.vertical },
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
      key: "gross_contribution_usd",
      header: "Gross Contribution",
      align: "right",
      sortable: true,
      sortValue: (r) => r.gross_contribution_usd,
      render: (r) => <span className={styles.moneyCell}>{formatMoney(r.gross_contribution_usd)}</span>,
    },
    {
      key: "roas",
      header: "ROAS",
      align: "right",
      sortable: true,
      sortValue: (r) => r.roas ?? -Infinity, // null (no spend) sorts BELOW a real loss, matching the roi column
      render: (r) => <span className={styles.moneyCell}>{formatRoas(r.roas)}</span>,
    },
    {
      key: "roi",
      header: "ROI",
      align: "right",
      sortable: true,
      sortValue: (r) => r.roi ?? -Infinity,
      render: (r) => <span className={styles.moneyCell}>{formatRoiPercent(r.roi)}</span>,
    },
    {
      key: "blended_roas",
      header: "Blended ROAS",
      align: "right",
      sortable: true,
      sortValue: (r) => r.blended_roas ?? -Infinity, // null (no spend) sorts BELOW a real loss, matching the roi column
      render: (r) => <span className={styles.moneyCell}>{formatRoas(r.blended_roas)}</span>,
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
      rowKey={(r) => r.vertical}
      emptyMessage="No revenue recorded by vertical for this day."
      caption="Revenue, ad spend, gross contribution, ROAS, ROI, blended ROAS and payment failures by vertical"
    />
  );
}
