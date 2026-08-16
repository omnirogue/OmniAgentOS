"use client";

import { EmptyState, ErrorState, Loading } from "../../design";
import { SuggestionCard } from "../steward";
import type { ApproveSuggestionResult, Suggestion } from "../steward";
import stewardStyles from "../steward/steward.module.css";

/** Reads the SAME `/api/suggestions` (open-scoped) + approve/dismiss decide
 * routes the standalone `/suggestions` page uses, via the shared
 * `SuggestionCard` component — no new endpoint, no new decision UI. */
export function SuggestionsTab({
  suggestions,
  loading,
  error,
  refresh,
  approve,
  dismiss,
}: {
  suggestions: Suggestion[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
  approve: (id: string, approvedBy: string) => Promise<ApproveSuggestionResult>;
  dismiss: (id: string, decidedBy: string, reason?: string) => Promise<Suggestion>;
}) {
  if (loading) return <Loading variant="skeleton" label="Loading suggestions…" lines={3} />;
  if (error) return <ErrorState message={error} onRetry={refresh} />;
  if (suggestions.length === 0) return <EmptyState title="Nothing here" message="No open suggestions." />;

  return (
    <div className={stewardStyles.stack}>
      {suggestions.map((suggestion) => (
        <SuggestionCard key={suggestion.id} suggestion={suggestion} onApprove={approve} onDismiss={dismiss} />
      ))}
    </div>
  );
}
