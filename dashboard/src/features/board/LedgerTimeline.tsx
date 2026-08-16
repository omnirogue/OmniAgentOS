"use client";

/**
 * LedgerTimeline (card drawer "Ledger" tab) — this card's session-ledger
 * event history, from `GET /api/ledger/tail?ref=task=<id>`, fetched by
 * useTaskDetail's own dedicated ledger effect (lazy-on-open, budgeted, and
 * decoupled from the rest of the panel — see that hook's docstring).
 *
 * `events` is `LedgerEvent[] | null`: `null` covers BOTH still-loading
 * (`loading=true`) and unavailable/timed-out (`loading=false`) — the two are
 * told apart by the `loading` prop, and neither is ever conflated with
 * genuine emptiness (`events === []`, loaded, zero rows). Rendering a
 * 503/timeout as "no ledger events" would be a lie the drawer tells the
 * operator, so each state gets its own distinct rendering:
 *   loading           -> a loading skeleton
 *   null              -> "Ledger unavailable — the session-ledger CLI did not answer."
 *   [] (loaded)       -> "No ledger events for this card."
 *   [...] (loaded)    -> the rows
 *
 * Rows arrive newest-last, exactly as the CLI orders them (ledger.py::
 * cmd_tail sorts ascending by `(at, id)` and returns the tail slice in that
 * order) — rendered here in the SAME order, never re-sorted client-side.
 * Each row renders `at · agent · event · summary`; the timestamp is a
 * semantic `<time>` element carrying the FULL ISO instant (year through the
 * UTC offset) in `dateTime`/`title`, with a full rendered text too (year and
 * seconds included, never abbreviated away) — an audit trail that silently
 * drops the year or the seconds is not trustworthy. Corrections (`note`
 * rows with `refs.corrects=<id>`) are attached to their target row BY THE
 * CLI (ledger.py::attach_corrections) and arrive as `_corrections` on the
 * row they correct — this renders exactly what it is given, never
 * re-deriving the attachment client-side.
 */

import { EmptyState, Loading } from "@/design";
import type { LedgerEvent } from "@/features/collab/types";
import styles from "./board.module.css";

function formatAt(at: string): string {
  const date = new Date(at);
  return Number.isNaN(date.getTime())
    ? at
    : date.toLocaleString([], {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
}

export function LedgerTimeline({
  events,
  loading = false,
}: {
  events: LedgerEvent[] | null;
  loading?: boolean;
}) {
  if (loading) {
    return <Loading variant="skeleton" label="Loading ledger events" lines={3} />;
  }
  if (events === null) {
    return <EmptyState message="Ledger unavailable — the session-ledger CLI did not answer." />;
  }
  if (!events.length) {
    return <EmptyState message="No ledger events for this card." />;
  }
  return (
    <div className={styles.ledgerList}>
      {events.map((event) => (
        <div key={event.id} className={styles.ledgerRow}>
          <span className={styles.ledgerMeta}>
            <time dateTime={event.at} title={event.at}>{formatAt(event.at)}</time> · {event.agent} · {event.event}
          </span>
          <span className={styles.ledgerSep}> · </span>
          <span className={styles.ledgerSummary}>{event.summary}</span>
          {event._corrections?.length ? (
            <div className={styles.ledgerCorrections}>
              {event._corrections.map((correction) => (
                <div key={correction.id} className={styles.ledgerCorrection}>
                  <span className={styles.ledgerMeta}>
                    <time dateTime={correction.at} title={correction.at}>{formatAt(correction.at)}</time> · correction
                  </span>
                  <span className={styles.ledgerSep}> · </span>
                  <span className={styles.ledgerSummary}>{correction.summary}</span>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}
