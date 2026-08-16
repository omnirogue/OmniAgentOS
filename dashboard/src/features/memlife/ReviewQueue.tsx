/**
 * Presentational review-queue surface for the three memlife queue states.
 *
 * Each state has a dedicated test id and role so tests (and operators) can tell
 * them apart. Collapsing store_unavailable into the empty branch is the named
 * counterfeit this component exists to prevent.
 */

import { Badge, Card, EmptyState, ErrorState, Section } from "@/design";
import type { MemlifeQueueView } from "./types";
import styles from "../reliability/improvements.module.css";

type Props = {
  state: MemlifeQueueView;
  onRetry?: () => void;
};

export function ReviewQueue({ state, onRetry }: Props) {
  if (state.kind === "store_unavailable") {
    // DECISIVE: missing store is an alert, never an empty queue.
    return (
      <Section title="Review queue">
        <Card padding="md">
          <div data-testid="memlife-queue-store-unavailable">
            <ErrorState
              title="Memory store unavailable"
              message="The memlife store is unavailable (store_unavailable). A missing store is not an empty queue — fix the store path or migrations before treating this as zero pending."
              onRetry={onRetry}
            />
          </div>
        </Card>
      </Section>
    );
  }

  if (state.kind === "error") {
    return (
      <Section title="Review queue">
        <Card padding="md">
          <ErrorState
            title="Could not load review queue"
            message={state.message}
            onRetry={onRetry}
          />
        </Card>
      </Section>
    );
  }

  // state.kind === "ok"
  if (state.pending === 0) {
    return (
      <Section title="Review queue">
        <Card padding="md">
          <div data-testid="memlife-queue-empty" role="status">
            <EmptyState
              title="Queue is empty"
              message="No candidates pending review. The store is reachable and the staged/reopened set is empty."
            />
          </div>
        </Card>
      </Section>
    );
  }

  return (
    <Section title={`Review queue (${state.pending})`}>
      <Card padding="md">
        <div data-testid="memlife-queue-pending" className={styles.card}>
          <div className={styles.cardHeader}>
            <div>
              <h3 className={styles.cardTitle}>
                {state.pending} candidate{state.pending === 1 ? "" : "s"} awaiting review
              </h3>
              <p className={styles.summary}>
                Staged and reopened claims wait here for a human graduate or reject
                decision. Unknown or unparseable statuses must never be shown as
                graduated.
              </p>
            </div>
            <Badge tone="warn" category="runState">
              pending: {state.pending}
            </Badge>
          </div>
        </div>
      </Card>
    </Section>
  );
}
