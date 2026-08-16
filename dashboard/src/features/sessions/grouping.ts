import type { Session } from "../../lib/contracts";

/** The five known companies for the sessions board, in the required display
 * order. Anything else — including a null/blank company — sorts into the
 * trailing "Other" bucket. */
export const KNOWN_COMPANIES = ["AcmeUni", "Globex", "Initech", "Hooli", "Ops"] as const;
export type KnownCompany = (typeof KNOWN_COMPANIES)[number];
export const OTHER_COMPANY = "Other";

/** `company_override` (the operator's manual correction) wins over the
 * collector-assigned `company` whenever both are present. Blank strings are
 * treated as absent. */
export function effectiveCompany(session: Pick<Session, "company" | "company_override">): string | null {
  const override = session.company_override?.trim();
  if (override) return override;
  const company = session.company?.trim();
  return company ? company : null;
}

function knownCompanyMatch(value: string): KnownCompany | null {
  return KNOWN_COMPANIES.find((known) => known.toLowerCase() === value.toLowerCase()) ?? null;
}

export interface SessionCompanyGroup {
  company: KnownCompany | typeof OTHER_COMPANY;
  sessions: Session[];
}

/**
 * Groups sessions by company into the five known companies (fixed order)
 * plus a trailing "Other" bucket for anything unmatched or unassigned.
 * Groups with zero sessions are dropped so an operator never sees empty
 * company sections.
 */
export function groupSessionsByCompany(sessions: Session[]): SessionCompanyGroup[] {
  const order: (KnownCompany | typeof OTHER_COMPANY)[] = [...KNOWN_COMPANIES, OTHER_COMPANY];
  const buckets = new Map<string, Session[]>();
  for (const key of order) buckets.set(key, []);
  for (const session of sessions) {
    const value = effectiveCompany(session);
    const key = value ? (knownCompanyMatch(value) ?? OTHER_COMPANY) : OTHER_COMPANY;
    buckets.get(key)!.push(session);
  }
  return order
    .map((company) => ({ company, sessions: buckets.get(company) ?? [] }))
    .filter((group) => group.sessions.length > 0);
}

/** Sessions the operator needs to act on right now — pinned above the
 * per-company grouping. Absent/null `attention_state` (older responses, or a
 * session with nothing to flag) is never treated as "needs you". */
export function sessionsNeedingAttention(sessions: Session[]): Session[] {
  return sessions.filter((session) => session.attention_state === "needs_input");
}

/** States that count as live work on the Sessions panel. Terminal states
 * (completed/failed/cancelled/killed) are history. needs_input always counts
 * as active regardless of state. */
export const ACTIVE_SESSION_STATES = new Set(["starting", "running", "awaiting_approval", "resuming"]);

export function isSessionActive(session: Session): boolean {
  return ACTIVE_SESSION_STATES.has(session.state) || session.attention_state === "needs_input";
}
