import { Card, EmptyState, Table, type TableColumn } from "@/design";
import { formatMoney, formatRoas } from "../format";
import styles from "../revenue.module.css";
import type { RevenueCampaignRow } from "../contracts";

export type CampaignTableProps = { rows: RevenueCampaignRow[] };

export function CampaignTable({ rows }: CampaignTableProps) {
  if (rows.length === 0) {
    return (
      <Card>
        <EmptyState title="No campaigns" message="No Meta campaign spend was attributed for this day." />
      </Card>
    );
  }

  const columns: TableColumn<RevenueCampaignRow>[] = [
    { key: "campaign_name", header: "Campaign", render: (r) => r.campaign_name },
    { key: "vertical", header: "Vertical", render: (r) => r.vertical },
    { key: "source", header: "Source", render: (r) => r.source },
    {
      key: "ad_spend_usd",
      header: "Spend",
      align: "right",
      sortable: true,
      sortValue: (r) => r.ad_spend_usd,
      render: (r) => <span className={styles.moneyCell}>{formatMoney(r.ad_spend_usd)}</span>,
    },
    {
      key: "roas",
      header: "ROAS",
      align: "right",
      sortable: true,
      sortValue: (r) => r.roas ?? -1,
      render: (r) => <span className={styles.moneyCell}>{formatRoas(r.roas)}</span>,
    },
    {
      key: "revenue_attributed_usd",
      header: "Attributed Revenue",
      align: "right",
      sortable: true,
      sortValue: (r) => r.revenue_attributed_usd,
      render: (r) => <span className={styles.moneyCell}>{formatMoney(r.revenue_attributed_usd)}</span>,
    },
  ];

  // Default view is spend-descending (biggest spend first); the Spend column
  // stays sortable so the operator can flip it or sort by another column.
  const sortedBySpendDesc = [...rows].sort((a, b) => b.ad_spend_usd - a.ad_spend_usd);

  return (
    <Table
      columns={columns}
      rows={sortedBySpendDesc}
      rowKey={(r) => r.campaign_id || `${r.vertical}:${r.source}:${r.campaign_name}`}
      emptyMessage="No Meta campaign spend was attributed for this day."
      caption="Meta campaigns by spend: name, spend, ROAS and attributed revenue"
    />
  );
}
