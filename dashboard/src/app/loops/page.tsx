"use client";

import { Page, PageHeader } from "@/design";
import { LoopHealthCard } from "@/features/loop-health/LoopHealthCard";
import { SystemLoopsSection } from "@/features/loops";

/**
 * `/loops` — "what runs on a schedule anywhere in my estate, and is it
 * healthy?" Two sections: DB-managed routines (reuses the existing
 * `LoopHealthCard` derivation verbatim, no duplication) and system loops
 * (launchd/remote/CSI jobs discovered off `GET /api/system-jobs`). Each
 * section owns its own live clock/poll — see `SystemLoopsSection` — rather
 * than sharing one `Date` frozen at first page render.
 */
export default function LoopsPage() {
  return (
    <Page>
      <PageHeader
        eyebrow="Automation"
        title="Loops"
        lead="Everything that runs on a schedule anywhere in the estate — managed DB routines and system-level jobs — and whether it's healthy."
      />
      <LoopHealthCard />
      <SystemLoopsSection />
    </Page>
  );
}
