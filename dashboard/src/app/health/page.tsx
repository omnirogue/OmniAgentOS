"use client";

import { useMemo, useState } from "react";
import { Card, EmptyState, ErrorState, Loading, Page, PageHeader } from "@/design";
import {
  CapabilityDetailDialog,
  EMPTY_FILTERS,
  HealthFilters,
  HealthTable,
  SummaryBar,
  countByStatus,
  filterCapabilities,
  useHealthData,
  type CapabilityHealth,
  type HealthFiltersState,
} from "@/components/health";

export default function HealthPage() {
  const { capabilities, generatedAt, loading, error, refresh } = useHealthData();
  const [filters, setFilters] = useState<HealthFiltersState>(EMPTY_FILTERS);
  const [selected, setSelected] = useState<CapabilityHealth | null>(null);

  // `capabilities` is already broken-first sorted by useHealthData — filtering
  // never re-sorts, so the default DOWN -> ... -> OK order survives filtering
  // with zero user interaction, matching the brief's D2 requirement.
  const visible = useMemo(() => filterCapabilities(capabilities ?? [], filters), [capabilities, filters]);
  const counts = useMemo(() => countByStatus(capabilities ?? []), [capabilities]);

  return (
    <Page>
      <PageHeader
        eyebrow="Capability map"
        title="Capability health"
        lead="Every automation, LLM loop, external service, and data store capmap watches — broken-first by default, so a DOWN capability never hides behind a scroll or a click."
        meta={generatedAt ? <span>Snapshot generated {new Date(generatedAt).toLocaleString()}</span> : undefined}
      />

      {loading && !capabilities ? (
        <Card>
          <Loading variant="skeleton" label="Loading capability health…" lines={6} />
        </Card>
      ) : null}
      {error ? <ErrorState message={`Could not load capability health: ${error}`} onRetry={() => void refresh()} /> : null}
      {!loading && !error && capabilities && capabilities.length === 0 ? (
        <Card>
          <EmptyState title="No capabilities registered" message="The capmap registry is empty." />
        </Card>
      ) : null}

      {capabilities && capabilities.length > 0 ? (
        <>
          <Card>
            <SummaryBar counts={counts} />
          </Card>
          <Card>
            <HealthFilters filters={filters} onChange={setFilters} />
          </Card>
          <Card padding="none">
            <HealthTable capabilities={visible} onSelect={setSelected} />
          </Card>
        </>
      ) : null}

      <CapabilityDetailDialog capabilityId={selected?.id ?? null} onClose={() => setSelected(null)} />
    </Page>
  );
}
