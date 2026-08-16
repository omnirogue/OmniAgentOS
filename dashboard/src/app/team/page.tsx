"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Button, Loading, Page, PageHeader, Section } from "@/design";
import { TaskDetailDrawer } from "@/features/board/TaskDetailDrawer";
import { AccountabilityStrip } from "@/features/team/AccountabilityStrip";
import { ScoreboardStrip } from "@/features/team/ScoreboardStrip";
import { TeamBoard } from "@/features/team/TeamBoard";
import { UnattributedInbox } from "@/features/team/UnattributedInbox";

/**
 * /team — Team Work OS (P5). Scoreboard strip (defensive; hides when the
 * parallel scoreboard package is not up yet) + the per-person board + a
 * collapsible unattributed-evidence inbox. Cards deep-link `?task=<id>`,
 * the same convention `/board` uses (`TaskDetailDrawer`, unmodified).
 */
function TeamPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [inboxOpen, setInboxOpen] = useState(false);

  const drawerTaskId = searchParams.get("task");

  const openTask = (taskId: string) => {
    router.push(`/team?task=${encodeURIComponent(taskId)}`, { scroll: false });
  };
  const closeTask = () => {
    router.replace("/team", { scroll: false });
  };

  return (
    <Page>
      <PageHeader
        eyebrow="Team"
        title="Team Work OS"
        lead="Per-person queues, verification, and evidence attribution for the people and agents working the board."
      />

      <ScoreboardStrip />

      <AccountabilityStrip />

      <Section title="Board">
        <TeamBoard onOpenTask={openTask} />
      </Section>

      <Section
        title="Unattributed evidence"
        actions={
          <Button variant="ghost" size="sm" aria-expanded={inboxOpen} onClick={() => setInboxOpen((current) => !current)}>
            {inboxOpen ? "Hide" : "Show"}
          </Button>
        }
      >
        {inboxOpen ? <UnattributedInbox /> : null}
      </Section>

      {drawerTaskId ? <TaskDetailDrawer taskId={drawerTaskId} onClose={closeTask} /> : null}
    </Page>
  );
}

export default function TeamPage() {
  return (
    <Suspense fallback={<Page><Loading variant="skeleton" label="Loading the team page" lines={6} /></Page>}>
      <TeamPageContent />
    </Suspense>
  );
}
