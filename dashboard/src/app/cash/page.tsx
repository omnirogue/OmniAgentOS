"use client";

import { Badge, Button, Card, EmptyState, ErrorState, Icon, Loading, Page, PageHeader, Pill, Section } from "@/design";
import {
  BalancesTable,
  DataQualityPanel,
  formatDay,
  formatEasternTimestamp,
  KpiTiles,
  MonthToDate,
  RevenueStrip,
  TransactionsTable,
  useCashDay,
} from "@/features/cash";
import styles from "@/features/cash/cash.module.css";

export default function CashPage() {
  const { data, loading, error, refresh } = useCashDay();

  // The API defaults `day` to yesterday ET (collectors run for the previous
  // day, so "today" almost always has no facts yet) — see features/cash/api.ts.
  // But the default day itself can still be empty (collector hasn't run yet,
  // fresh deployment, etc.): by_account rows come 1:1 from that day's bank
  // facts, so an empty by_account means no facts at all were stored for this
  // day (the backend's own data_quality note says exactly this). Show that
  // plainly instead of a page of $0.00 KPI tiles that would otherwise read
  // as "you have no money," which may simply be untrue.
  const isEmptyDay = !!data && data.by_account.length === 0;

  const metaRow = data ? (
    <div className={styles.metaRow}>
      <span>{formatDay(data.day)}</span>
      <Badge tone={data.totals.net_flow_usd >= 0 ? "ok" : "warn"}>
        {data.totals.net_flow_usd >= 0 ? "Positive" : "Negative"} net flow
      </Badge>
      <span>
        as of {formatEasternTimestamp(data.generated_at)}, {data.timezone}
      </span>
      <Pill tone="neutral">Refreshes hourly</Pill>
    </div>
  ) : null;

  return (
    <Page className={styles.cashPage}>
      <PageHeader
        eyebrow="Steward"
        title="Cash"
        lead="Bank balances across all accounts, money deposited, and TRUE cash expenses (actual debits out of your bank accounts). This is a cash-flow view — money that actually moved — not accrued cost, COGS, or ad spend."
        meta={metaRow}
      />

      {/* F07: the daily revenue summary sits above the banking report, at
          top of page, as its own independently-loading/erroring strip. */}
      <RevenueStrip />

      {loading && !data ? (
        <Card>
          <Loading variant="skeleton" label="Loading cash…" lines={4} />
        </Card>
      ) : null}
      {error ? <ErrorState message={`Could not load cash: ${error}`} onRetry={() => void refresh()} /> : null}

      {data && isEmptyDay ? (
        <Card>
          <EmptyState
            icon={<Icon name="inbox" size={22} />}
            title={`No cash activity recorded yet for ${formatDay(data.day)}`}
            message={
              data.data_quality[0]?.message ??
              "The collector may not have run yet for this day — check back after it completes."
            }
            action={
              <Button size="sm" onClick={() => void refresh()}>
                Refresh
              </Button>
            }
          />
        </Card>
      ) : null}

      {data ? <MonthToDate mtd={data.month_to_date} /> : null}

      {data && !isEmptyDay ? (
        <>
          <KpiTiles totals={data.totals} />

          <Section className={styles.cashSection} eyebrow="Accounts" title="Balances by Account">
            <div className={styles.tableScroll}>
              <BalancesTable rows={data.by_account} />
            </div>
          </Section>

          <Section
            className={styles.cashSection}
            eyebrow="Movement"
            title="Largest Transactions"
            description="Biggest deposits and expenses of the day, by absolute amount."
          >
            <div className={styles.tableScroll}>
              <TransactionsTable rows={data.largest_transactions} />
            </div>
          </Section>
        </>
      ) : null}

      {/* Always rendered once we have a day's payload — the honesty surface
          must never be hidden, even when the day above turned out empty. */}
      {data ? <DataQualityPanel notes={data.data_quality} coverage={data.coverage} /> : null}
    </Page>
  );
}
