"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { Badge, Loading, Page, PageHeader, Tabs, type TabItem } from "../../design";
import { AlertsTab, ApprovalsTab, BriefingsTab, DecisionsTab, TabLabel, useApprovalsFeed } from "../../features/inbox";
import { useDecisions } from "../../features/decisions";
import { useProjectContext } from "../../features/projects";
import { useAlerts, useBriefing } from "../../features/steward";
import inboxStyles from "../../features/inbox/inbox.module.css";

/** The Inbox — everything that wants an operator decision or a look, in one
 * tabbed shell. Approvals is the default tab and keeps 100% of its former
 * standalone-page behaviour. Alerts/Briefings each read their EXISTING
 * endpoint via the shared `features/steward` hooks. Decisions is the
 * Executive Decision Center's owner-scoped queue — inserted AFTER Approvals so
 * the default surface is unchanged; a Slack DM deep-links to it via
 * `/approvals?tab=decisions`.
 *
 * F08: the Suggestions tab was dormant (no live producer ever populated it)
 * and is removed outright — four tabs now: Approvals/Decisions/Alerts/
 * Briefings. */
function InboxContent() {
  const approvalsFeed = useApprovalsFeed();
  const alertsFeed = useAlerts("open");
  const briefingFeed = useBriefing();
  const decisionsFeed = useDecisions();
  const { activeProject } = useProjectContext();
  const searchParams = useSearchParams();

  const unackedBriefings = briefingFeed.history.filter((briefing) => !briefing.acked_at).length;

  const items: TabItem[] = [
    {
      id: "approvals",
      label: <TabLabel label="Approvals" count={approvalsFeed.pending.length} />,
      content: <ApprovalsTab feed={approvalsFeed} />,
    },
    {
      // Badge counts only the visible queue (urgent + needs-me); MAYBE and
      // snoozed are excluded by construction (§10.3).
      id: "decisions",
      label: <TabLabel label="Decisions" count={decisionsFeed.badgeCount} />,
      content: <DecisionsTab feed={decisionsFeed} />,
    },
    {
      id: "alerts",
      label: <TabLabel label="Alerts" count={alertsFeed.alerts.length} />,
      content: (
        <AlertsTab
          alerts={alertsFeed.alerts}
          loading={alertsFeed.loading}
          error={alertsFeed.error}
          refresh={() => void alertsFeed.refresh()}
          ack={alertsFeed.ack}
        />
      ),
    },
    {
      id: "briefings",
      label: <TabLabel label="Briefings" count={unackedBriefings} />,
      content: (
        <BriefingsTab
          history={briefingFeed.history}
          loading={briefingFeed.loading}
          error={briefingFeed.error}
          refresh={() => void briefingFeed.refresh()}
          ack={briefingFeed.ack}
        />
      ),
    },
  ];

  // Deep link: `?tab=<id>` selects a starting tab (Approvals stays the default
  // when absent/unknown), so a Slack DM can land the owner on Decisions.
  const requested = searchParams?.get("tab") ?? null;
  const defaultTab = items.some((item) => item.id === requested) ? requested! : "approvals";

  return (
    <Page>
      <PageHeader
        eyebrow="Governance"
        title="Inbox"
        lead="Everything waiting on you — approvals, decisions, alerts, and briefings — in one place. Approvals keeps its full decision workflow as the default tab."
        actions={
          <div className={inboxStyles.headerActions}>
            {activeProject ? <Badge tone="challenger">Project: {activeProject.name}</Badge> : null}
            <Badge tone={approvalsFeed.connected ? "ok" : approvalsFeed.streamError ? "danger" : "neutral"}>
              {approvalsFeed.connected ? "Live events connected" : approvalsFeed.streamError ? "Live events failed" : "Live events reconnecting…"}
            </Badge>
          </div>
        }
      />

      <Tabs aria-label="Inbox" items={items} defaultValue={defaultTab} />
    </Page>
  );
}

export default function ApprovalsPage() {
  return (
    <Suspense fallback={<Page><Loading label="Loading inbox…" /></Page>}>
      <InboxContent />
    </Suspense>
  );
}
