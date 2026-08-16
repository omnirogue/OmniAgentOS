"use client";

import { Card, ErrorState, Loading, Page, PageHeader } from "@/design";
import {
  AccountsStrip,
  AlertsSection,
  GateSection,
  LandingsSection,
  LoopsSection,
  QueueSection,
  RecyclerSection,
  useStatus,
} from "@/features/status";
import statusStyles from "@/features/status/status.module.css";

/**
 * `/` — Status, mission control (10-page IA rebuild, DASHBOARD-REDO-DESIGN.md).
 * Every section reads `GET /api/local/status`, a same-origin Next server
 * route that assembles a JSON status object straight from ground truth on
 * this box (tmux, loop/gate/recycler logs, queue.json top-level keys, `git
 * log origin/main`, ALERTS.md), polled every 20s. No section fakes a
 * healthy default when its own source failed — each renders its own
 * `{ ok: false, error }` state instead, independent of the others.
 */
export default function StatusPage() {
  const { data, loading, error, refresh, lastUpdated, refreshError } = useStatus();
  const now = Date.now();

  // No `data` yet AND the fetch that would have produced it failed -- the
  // page has nothing honest to show but the error itself.
  const initialLoadFailed = !data && Boolean(error);
  // F04: `data` is present (from a prior successful load) but the MOST
  // RECENT poll failed -- the payload on screen is stale and that must be
  // visible, not silently rendered as if it were still fresh.
  const showStaleBanner = Boolean(data) && Boolean(refreshError);

  return (
    <Page>
      <PageHeader
        eyebrow="Mission control"
        title="Status"
        lead="Loop liveness, the merge gate, the loop queue, landings, and the hang recycler — read live from this box, not from a database that might be stale."
      />

      {loading && !data && !error ? (
        <Card>
          <Loading label="Loading status…" />
        </Card>
      ) : null}

      {initialLoadFailed ? (
        <Card>
          <ErrorState message={`Could not load status: ${error}`} onRetry={() => void refresh()} />
        </Card>
      ) : null}

      {showStaleBanner ? (
        <div className={statusStyles.queueBanner} role="status">
          <span className={statusStyles.queueBannerTitle}>Data is stale</span>
          <span>
            Last successful update {lastUpdated ? new Date(lastUpdated).toLocaleTimeString() : "unknown"} — the
            most recent refresh failed: {refreshError}
          </span>
        </div>
      ) : null}

      {data ? (
        <>
          <LoopsSection loops={data.loops} now={now} />
          <GateSection gate={data.gate} />
          <QueueSection queue={data.queue} />
          <LandingsSection landings={data.landings} />
          <RecyclerSection recycler={data.recycler} />
          <AlertsSection alerts={data.alerts} />
        </>
      ) : null}

      <AccountsStrip />
    </Page>
  );
}
