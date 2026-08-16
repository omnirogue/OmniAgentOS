import { Badge, Card, EmptyState, Pill, Section, Stat, cx } from "@/design";
import { formatMoney } from "../format";
import { formatCollectedRoas, sourceName } from "../logic";
import styles from "../companies.module.css";
import type { CompaniesPortfolio, CompanyGoal, CompanyView } from "../contracts";

export type CompaniesOverviewProps = { portfolio: CompaniesPortfolio; className?: string };

function goalTone(status: CompanyGoal["status"]): "ok" | "warn" | "neutral" {
  if (status === "achieved") return "ok";
  if (status === "paused") return "warn";
  return "neutral";
}

function Goals({ goals }: { goals: CompanyGoal[] }) {
  return (
    <div className={styles.detailGroup}>
      <p className={styles.detailLabel}>Goals</p>
      {goals.length ? (
        <ul className={styles.goalList}>
          {goals.map((goal) => (
            <li key={goal.id} className={styles.goal}>
              <span className={styles.goalTitle}>{goal.title}</span>
              <span className={styles.goalMeta}>
                <span>{goal.horizon === "long_term" ? "Long term" : "Short term"}</span>
                <Badge tone={goalTone(goal.status)}>{goal.status}</Badge>
              </span>
            </li>
          ))}
        </ul>
      ) : <p className={styles.emptyText}>No goals recorded.</p>}
    </div>
  );
}

function Products({ company }: { company: CompanyView["company"] }) {
  return (
    <div className={styles.detailGroup}>
      <p className={styles.detailLabel}>Products</p>
      <div className={styles.chips}>
        {company.products.length ? company.products.map((product) => <Pill key={product.id || product.slug} tone="accent">{product.name}</Pill>) : <span className={styles.emptyText}>No products recorded.</span>}
      </div>
    </div>
  );
}

function CollectorHealth({ sources }: { sources: CompanyView["sources"] }) {
  return (
    <div className={styles.detailGroup}>
      <p className={styles.detailLabel}>Collectors</p>
      <div className={styles.chips}>
        {sources.length ? sources.map((source) => <CollectorPill key={source.key} source={source} />) : <span className={styles.emptyText}>No collector status reported.</span>}
      </div>
    </div>
  );
}

function CollectorPill({ source }: { source: CompanyView["sources"][number] }) {
  // An unparseable last_ok_at must read as STALE, not fresh: NaN > threshold
  // is false, which silently took the healthy branch (review R5).
  const okAgeMs = source.last_ok_at === null ? null : Date.now() - new Date(source.last_ok_at).getTime();
  const isStale = source.status === "ok"
    && okAgeMs !== null
    && (!Number.isFinite(okAgeMs) || okAgeMs > 3 * 24 * 60 * 60 * 1000);
  if (isStale) {
    return <Pill tone="warn">{sourceName(source)} ok but stale (last day {source.last_day ?? source.last_ok_at})</Pill>;
  }
  if (source.status === "ok") return <Pill tone="ok">{sourceName(source)} ok</Pill>;
  if (source.status === "failed") return <Pill tone="danger">FAILED {sourceName(source)}</Pill>;
  return <Pill tone="warn">{sourceName(source)} {source.status}</Pill>;
}

function BrandCard({ view }: { view: CompanyView }) {
  const { vertical } = view;
  // The shared contract is null-honest now — no local re-widening needed.
  const honestVertical = vertical;
  return (
    <Card raised padding="lg" className={styles.brandCard}>
      <div className={styles.cardTitleRow}>
        <h3 className={styles.cardTitle}>{view.company.name}</h3>
        <span className={styles.period}>7 days</span>
      </div>
      <div className={styles.metrics}>
        <Stat label="Revenue" value={formatMoney(honestVertical?.revenue_usd)} tone="accent" />
        <Stat label="Ad spend" value={formatMoney(honestVertical?.ad_spend_usd)} tone="warn" />
        <Stat label="ROAS (collected)" value={formatCollectedRoas(honestVertical?.roas_collected ?? null)} tone="ok" />
      </div>
      <Goals goals={view.goals} />
      <Products company={view.company} />
      <CollectorHealth sources={view.sources} />
      <p className={styles.emptyText}>
        Ops docs:{" "}
        <a
          href={`file:///Users/youruser/Work/${encodeURIComponent(view.company.name)}/Operations`}
          className={styles.opsLink}
          title="Local folder — opens in a file-capable context; the path is also selectable text"
        >
          <code>~/{view.company.name}/Operations</code>
        </a>
      </p>
    </Card>
  );
}

function PlatformRow({ view }: { view: CompanyView }) {
  return (
    <Card raised padding="lg" className={styles.platformRow}>
      {/* No <Stat> revenue block by design — not a revenue vertical. */}
      <div>
        <p className={styles.detailLabel}>Platform</p>
        <h3 className={styles.cardTitle}>{view.company.name}</h3>
      </div>
      <Goals goals={view.goals} />
      <Products company={view.company} />
      <CollectorHealth sources={view.sources} />
    </Card>
  );
}

export function CompaniesOverview({ portfolio, className }: CompaniesOverviewProps) {
  return (
    <div className={cx(styles.overview, className)}>
      <Section eyebrow="Brands" title="Operating companies" description="Revenue facts cover the seven most recently completed Eastern-calendar days.">
        {portfolio.brands.length ? <div className={styles.brandGrid}>{portfolio.brands.map((view) => <BrandCard key={view.company.id} view={view} />)}</div> : (
          <Card><EmptyState title="No operating companies found" message="The organization catalog did not return any of the four branded companies." /></Card>
        )}
      </Section>

      {portfolio.unattributedGlobal ? <p className={styles.unattributed}>unattributed: global {formatMoney(portfolio.unattributedGlobal.revenue_usd)}</p> : null}

      {portfolio.platform ? (
        <Section eyebrow="Platform" title="OmniAgentOS">
          <PlatformRow view={portfolio.platform} />
        </Section>
      ) : null}
    </div>
  );
}
