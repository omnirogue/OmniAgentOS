import type { BadgeTone } from "@/design/Badge";
import { CANDIDATE_STATUSES, type KnownCandidateStatus } from "./types";

export interface CandidateStatusPresentation {
  label: string;
  tone: BadgeTone;
  /** True only for the known graduated path — never for unknown/absent. */
  isSuccess: boolean;
}

const KNOWN: Record<KnownCandidateStatus, CandidateStatusPresentation> = {
  staged: { label: "STAGED", tone: "warn", isSuccess: false },
  graduated: { label: "GRADUATED", tone: "completed", isSuccess: true },
  rejected: { label: "REJECTED", tone: "failed", isSuccess: false },
  reopened: { label: "REOPENED", tone: "challenger", isSuccess: false },
  quarantined: { label: "QUARANTINED", tone: "danger", isSuccess: false },
};

const UNKNOWN: CandidateStatusPresentation = {
  label: "UNKNOWN",
  tone: "warn",
  isSuccess: false,
};

function isKnown(value: string): value is KnownCandidateStatus {
  return (CANDIDATE_STATUSES as readonly string[]).includes(value);
}

/**
 * Map a candidate status to a badge presentation.
 *
 * Absent, empty, or unrecognised values render **UNKNOWN** with a non-success
 * tone. Never bucket unknowns into graduated/completed — that is the same class
 * of lie as /swarm painting cancelled runs as COMPLETED.
 */
export function candidateStatusPresentation(
  status: string | null | undefined,
): CandidateStatusPresentation {
  if (status == null) return UNKNOWN;
  const normalized = status.trim().toLowerCase();
  if (!normalized) return UNKNOWN;
  if (isKnown(normalized)) return KNOWN[normalized];
  return UNKNOWN;
}
