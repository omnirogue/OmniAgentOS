"use client";

import { Badge, Card, EmptyState, ErrorState, Icon, Loading, Table, type TableColumn } from "@/design";
import { formatDateOnly } from "@/lib/format";
import { useWeakspots } from "./hooks";
import { allSourcesAvailable, unavailableSources, weakspotTone } from "./tiles";
import type { WeakspotItem, WeakspotSourceCategory } from "./types";
import styles from "./testing.module.css";

const CATEGORY_LABELS: Record<string, string> = {
  northstar: "North Star",
  memory: "Memory",
  diagnostics: "Diagnostics",
  suite: "Suite",
};

/**
 * Shared "coverage incomplete" notice — same helper (`unavailableSources`)
 * and same list styling whether it renders as the primary content (the
 * empty-with-gaps state) or as a small notice above an otherwise-populated
 * table (grok T2-R2-N3: the ranking can have items AND still be missing a
 * source — an incomplete ranking is incomplete either way).
 */
function CoverageGapList({ gaps }: { gaps: Array<{ category: WeakspotSourceCategory; reason: string | null }> }) {
  return (
    <ul className={styles.sourceGapList}>
      {gaps.map((gap) => (
        <li key={gap.category}>
          <span className={styles.mono}>{CATEGORY_LABELS[gap.category] ?? gap.category}</span>
          {gap.reason ? `: ${gap.reason}` : null}
        </li>
      ))}
    </ul>
  );
}

const columns: TableColumn<WeakspotItem>[] = [
  {
    key: "category",
    header: "Category",
    sortable: true,
    render: (row) => CATEGORY_LABELS[row.category] ?? row.category,
    sortValue: (row) => row.category,
  },
  {
    key: "id",
    header: "ID",
    sortable: true,
    render: (row) => (
      <span className={styles.mono}>
        {row.id}
        {row.gate ? <span className={styles.gateFlag}> · gate</span> : null}
      </span>
    ),
    sortValue: (row) => row.id,
  },
  {
    key: "status",
    header: "Status",
    sortable: true,
    render: (row) => (
      <Badge category="result" tone={weakspotTone(row.status)}>
        {row.status}
      </Badge>
    ),
    sortValue: (row) => row.status,
  },
  {
    key: "detail",
    header: "Detail",
    render: (row) => <span className={styles.detailCell}>{row.detail}</span>,
  },
  {
    key: "since",
    header: "Since",
    sortable: true,
    // `since` is date-only (YYYY-MM-DD) — formatDateOnly parses the y/m/d
    // digits directly rather than going through `new Date()`, which would
    // parse a bare date as UTC midnight and shift a day in negative-offset
    // timezones (grok T2-N4).
    render: (row) => <span className={styles.mono}>{formatDateOnly(row.since)}</span>,
    sortValue: (row) => row.since,
  },
];

/**
 * Weak spots table — ranked failures/mechanics-refusals/stale cells across
 * every category, from GET /api/testobs/weakspots. The backend owns the
 * ranking and item order entirely (grok T2-N3) — items render in server
 * order, never re-sorted client-side (the Table's own column-header sort is
 * still available as an explicit user action).
 */
export function WeakSpotsPanel() {
  const { items, sources, status, error, refresh } = useWeakspots();

  if (status === "loading") {
    return (
      <Card>
        <div className={styles.weakspotsHead}>
          <h3 className={styles.trendTitle}>Weak spots</h3>
        </div>
        <Loading variant="skeleton" lines={5} label="Loading weak spots" />
      </Card>
    );
  }

  if (status === "error") {
    return (
      <Card>
        <div className={styles.weakspotsHead}>
          <h3 className={styles.trendTitle}>Weak spots</h3>
        </div>
        <ErrorState message={error ?? "Could not load weak spots"} onRetry={refresh} />
      </Card>
    );
  }

  if (status === "empty") {
    // grok T2-M2: an empty ranking only reads as a genuine "nothing ranked"
    // all-clear when every source that feeds it is available. If a source
    // (northstar/memory/diagnostics) is down, an empty list is a coverage
    // gap, not a clean bill of health — render a neutral state naming which
    // sources are missing and why, never the reassuring checkmark.
    if (allSourcesAvailable(sources)) {
      return (
        <Card>
          <div className={styles.weakspotsHead}>
            <h3 className={styles.trendTitle}>Weak spots</h3>
          </div>
          <EmptyState
            icon={<Icon name="checkCircle" size={20} />}
            title="Nothing ranked"
            message="No failing, refused, or stale test cells in the current window."
          />
        </Card>
      );
    }

    const gaps = unavailableSources(sources);
    return (
      <Card>
        <div className={styles.weakspotsHead}>
          <h3 className={styles.trendTitle}>Weak spots</h3>
        </div>
        <EmptyState
          icon={<Icon name="alertTriangle" size={20} />}
          title="Coverage incomplete"
          message="Nothing is ranked, but the ranking itself is incomplete — the following sources are unavailable:"
        />
        <CoverageGapList gaps={gaps} />
      </Card>
    );
  }

  // grok T2-R2-N3: the ready (non-empty) path previously never consulted
  // `sources` at all — a ranking with real items can still be missing a
  // source (e.g. memory down while northstar/diagnostics report fine), and
  // that gap was silently invisible. Render the same neutral notice above
  // the table; the table itself still renders in full either way.
  const gaps = allSourcesAvailable(sources) ? [] : unavailableSources(sources);

  return (
    <Card>
      <div className={styles.weakspotsHead}>
        <h3 className={styles.trendTitle}>Weak spots</h3>
        <span className={styles.weakspotsSummary}>{items.length} ranked</span>
      </div>
      {gaps.length > 0 ? (
        <div className={styles.coverageNotice}>
          <div className={styles.coverageNoticeHead}>
            <Icon name="alertTriangle" size={14} />
            <span>Coverage incomplete — the following sources are unavailable:</span>
          </div>
          <CoverageGapList gaps={gaps} />
        </div>
      ) : null}
      <Table
        columns={columns}
        rows={items}
        rowKey={(row) => `${row.category}:${row.id}`}
        emptyMessage="Nothing ranked"
        caption="Ranked test failures, mechanics refusals, and stale cells across all categories, in server order"
      />
    </Card>
  );
}
