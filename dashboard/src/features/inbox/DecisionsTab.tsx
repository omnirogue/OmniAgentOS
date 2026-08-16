"use client";

import { useState } from "react";
import { Badge, Card, EmptyState, ErrorState, Loading, Section } from "../../design";
import { DecisionCard } from "../decisions/DecisionCard";
import type { UseDecisions } from "../decisions/hooks";
import type { Decision } from "../decisions/types";
import decisionStyles from "../decisions/decisions.module.css";

/** The Executive Decision Center tab inside the Inbox. The page shell owns the
 * single `useDecisions()` instance and passes it here as props (mirrors how the
 * Alerts/Suggestions/Briefings tabs receive their steward feed). */
export function DecisionsTab({ feed }: { feed: UseDecisions }) {
  const { groups, loading, error, decide, refresh } = feed;
  const total = groups.urgent.length + groups.needsOwner.length + groups.maybe.length + groups.snoozed.length;

  const decideFn = (id: string, body: Parameters<typeof decide>[1]) => decide(id, body);

  return (
    <div>
      {loading && total === 0 ? <Loading label="Loading decisions…" /> : null}
      {error ? <ErrorState message={`Could not load decisions: ${error}`} onRetry={() => void refresh()} /> : null}

      {!loading && !error && total === 0 ? (
        <Card>
          <EmptyState message="Nothing needs a decision right now." />
        </Card>
      ) : null}

      {groups.urgent.length > 0 ? (
        <Section title={`Urgent (${groups.urgent.length})`}>
          <div className={decisionStyles.cardList}>
            {groups.urgent.map((decision) => (
              <DecisionCard key={decision.id} decision={decision} onDecide={decideFn} />
            ))}
          </div>
        </Section>
      ) : null}

      {groups.needsOwner.length > 0 ? (
        <Section title={`Needs me (${groups.needsOwner.length})`}>
          <div className={decisionStyles.cardList}>
            {groups.needsOwner.map((decision) => (
              <DecisionCard key={decision.id} decision={decision} onDecide={decideFn} />
            ))}
          </div>
        </Section>
      ) : null}

      {/* MAYBE review — an in-tab COLLAPSED section, never a separate tab/page,
          excluded from the badge count (§10.3). */}
      <CollapsibleReview
        label={`Maybe — ${groups.maybe.length} held back for review`}
        decisions={groups.maybe}
        onDecide={decideFn}
      />

      {groups.snoozed.length > 0 ? (
        <CollapsibleReview
          label={`Snoozed — ${groups.snoozed.length}`}
          decisions={groups.snoozed}
          onDecide={decideFn}
        />
      ) : null}
    </div>
  );
}

function CollapsibleReview({
  label,
  decisions,
  onDecide,
}: {
  label: string;
  decisions: Decision[];
  onDecide: (id: string, body: Parameters<UseDecisions["decide"]>[1]) => Promise<Decision>;
}) {
  const [open, setOpen] = useState(false);
  if (decisions.length === 0) return null;
  return (
    <div className={decisionStyles.section}>
      <button
        type="button"
        className={decisionStyles.collapsibleHead}
        aria-expanded={open}
        onClick={() => setOpen((prev) => !prev)}
      >
        <Badge tone="neutral">{open ? "▾" : "▸"}</Badge>
        <span>{label}</span>
      </button>
      {open ? (
        <div className={decisionStyles.cardList}>
          {decisions.map((decision) => (
            <DecisionCard key={decision.id} decision={decision} onDecide={onDecide} compact />
          ))}
        </div>
      ) : null}
    </div>
  );
}
