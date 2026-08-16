"use client";

import { Card, ErrorState, Loading, Page, PageHeader } from "@/design";
import { CompaniesOverview, formatEasternTimestamp, useCompanies } from "@/features/companies";
import styles from "@/features/companies/companies.module.css";

export default function CompaniesPage() {
  const { data, loading, error, refresh } = useCompanies();

  return (
    <Page>
      <PageHeader
        eyebrow="Portfolio"
        title="Companies"
        lead="Seven-day revenue and advertising performance alongside each brand's current goals, products, and collector health."
        meta={data ? <span>Data generated {formatEasternTimestamp(data.generated_at)}</span> : undefined}
      />

      {loading && !data ? (
        <Card>
          <Loading variant="skeleton" label="Loading companies…" lines={6} />
        </Card>
      ) : null}
      {error ? <ErrorState message={`Could not load companies: ${error}`} onRetry={() => void refresh()} /> : null}
      {data ? <CompaniesOverview portfolio={data} className={styles.overview} /> : null}
    </Page>
  );
}
