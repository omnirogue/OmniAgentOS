import type { Session } from "../../lib/contracts";

/** Fields the sessions board can edit inline (title, company). */
export type OverridableField = "title" | "company_override";

interface OverrideEntry {
  value: string | null;
  /** Monotonic id of the request that produced this optimistic value. Lets
   * `settleOverride` tell "the request I'm settling is still the current
   * edit for this field" apart from "a newer edit on the same field has
   * since superseded me" — an older save completing must never clobber a
   * newer save's still-in-flight optimistic value. */
  requestId: number;
}

export type SessionOverrides = Record<string, Partial<Record<OverridableField, OverrideEntry>>>;

/** Applies an optimistic value for one field of one session. Overrides for
 * OTHER fields on the same session (e.g. a still-saving title edit while a
 * company edit starts) are preserved untouched. */
export function applyOverride(
  overrides: SessionOverrides,
  sessionId: string,
  field: OverridableField,
  value: string | null,
  requestId: number,
): SessionOverrides {
  return {
    ...overrides,
    [sessionId]: { ...overrides[sessionId], [field]: { value, requestId } },
  };
}

/**
 * Clears the optimistic override for one field of one session, but ONLY if
 * `requestId` still matches the entry stored there. A wholesale
 * `delete overrides[sessionId]` (the pre-fix behavior) wiped a DIFFERENT
 * field's still-in-flight edit whenever any one field's save settled first —
 * e.g. a title save completing erased a still-saving company edit on the
 * same row. Per-field settling avoids that; the requestId check additionally
 * stops an older request's completion from clearing a NEWER edit that was
 * started on the same field before the older one finished.
 */
export function settleOverride(
  overrides: SessionOverrides,
  sessionId: string,
  field: OverridableField,
  requestId: number,
): SessionOverrides {
  const entry = overrides[sessionId]?.[field];
  if (!entry || entry.requestId !== requestId) return overrides;
  const rest = { ...overrides[sessionId] };
  delete rest[field];
  if (Object.keys(rest).length === 0) {
    const others = { ...overrides };
    delete others[sessionId];
    return others;
  }
  return { ...overrides, [sessionId]: rest };
}

/** Merges any pending optimistic overrides onto a session for display. */
export function resolveSessionOverrides(session: Session, overrides: SessionOverrides): Session {
  const entry = overrides[session.id];
  if (!entry) return session;
  return {
    ...session,
    ...(entry.title ? { title: entry.title.value } : {}),
    ...(entry.company_override ? { company_override: entry.company_override.value } : {}),
  };
}
