/**
 * Governed improvement proposal display mapper + self-approval guards.
 *
 * Design: docs/ui-redesign/improvements-governed-evidence-design.md §2.2–2.3.
 * Pure module — no React, no fetch. This is the ONLY place that reads nested
 * `proposal_json` keys for the improvements page, so the UI and the tests
 * share one projection of the governed evidence fields.
 */
import type { ApiImprovement } from "../../lib/reliabilityContracts";

const NOT_PROVIDED = "Not provided";
const REPORT_EXCERPT_LIMIT = 500;

export interface GovernedProposalView {
  evidence: { items: string[]; emptyLabel: string };
  expectedBenefit: string;
  scope: { changeType: string; paths: string[]; kind: string; origin: string };
  risks: {
    level: number | null;
    reasons: string[];
    restartRequired: boolean | null;
    narrative: string;
  };
  approval: { createdBy: string; decidedBy: string | null; status: string };
  execution: { goal: string; branch: string; plan: string[] };
  verification: {
    sandboxPassed: boolean | null;
    report: string;
    riskTier: string;
    votes: Array<[string, string]>;
  };
  rollback: { pointId: string | null; appliedSha: string | null };
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => text(item)).filter((item) => item.length > 0);
}

/** files[] / config_edits[] entries may be plain strings or {path} records. */
function pathList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const paths: string[] = [];
  for (const entry of value) {
    const path = typeof entry === "string" ? entry.trim() : text(record(entry).path);
    if (path) paths.push(path);
  }
  return paths;
}

function firstText(...values: unknown[]): string {
  for (const value of values) {
    const t = text(value);
    if (t) return t;
  }
  return "";
}

function boolOrNull(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

/** Project an ApiImprovement into the eight governed card sections. */
export function toGovernedProposalView(improvement: ApiImprovement): GovernedProposalView {
  const proposal = record(improvement.proposal_json);
  const sandbox = record(improvement.sandbox_json);
  const votesSummary = record(improvement.votes_summary_json);

  const evidenceItems: string[] = [];
  const rootCause = text(improvement.root_cause);
  if (rootCause) evidenceItems.push(rootCause);
  const before = firstText(improvement.before, proposal.before);
  if (before) evidenceItems.push(`Before: ${before}`);
  const after = firstText(improvement.after, proposal.after);
  if (after) evidenceItems.push(`After: ${after}`);
  evidenceItems.push(...stringList(proposal.plan));
  const repro = text(proposal.repro);
  if (repro) evidenceItems.push(repro);

  // Favourable-absence guard: a missing/unparseable risk_level must never
  // silently read as "L0 / safe". Only a genuine finite number counts; every
  // other shape (undefined, null, NaN-producing string, object) renders as
  // unknown so the operator is not falsely reassured. Widen to `unknown`
  // first — the API contract types this as `number`, but a live backend
  // payload can still hand us a missing/malformed value at runtime.
  const rawRiskLevel: unknown = improvement.risk_level;
  const parsedRiskLevel = Number(rawRiskLevel);
  const riskLevel: number | null =
    typeof rawRiskLevel === "number" && Number.isFinite(rawRiskLevel)
      ? rawRiskLevel
      : typeof rawRiskLevel === "string" &&
          rawRiskLevel.trim() !== "" &&
          Number.isFinite(parsedRiskLevel)
        ? parsedRiskLevel
        : null;

  const scopePaths = [
    ...pathList(proposal.files),
    ...pathList(proposal.config_edits),
  ].filter((path, index, all) => all.indexOf(path) === index);

  const report = text(sandbox.report);

  return {
    evidence: { items: evidenceItems, emptyLabel: NOT_PROVIDED },
    expectedBenefit:
      firstText(
        proposal.expected_impact,
        proposal.expected_benefit,
        proposal.predicted_impact,
        improvement.summary,
      ) || NOT_PROVIDED,
    scope: {
      changeType: text(proposal.change_type) || NOT_PROVIDED,
      paths: scopePaths,
      kind: improvement.kind,
      origin: improvement.origin,
    },
    risks: {
      level: riskLevel,
      reasons: stringList(sandbox.risk_reasons),
      restartRequired: boolOrNull(proposal.restart_required ?? sandbox.restart_required),
      narrative: firstText(proposal.risk_hint, proposal.risk_parse_error) || NOT_PROVIDED,
    },
    approval: {
      createdBy: improvement.created_by,
      decidedBy: text(improvement.decided_by) || null,
      status: String(improvement.status),
    },
    execution: {
      goal: firstText(proposal.goal, improvement.title) || NOT_PROVIDED,
      branch: firstText(proposal.branch, proposal.git_branch) || NOT_PROVIDED,
      plan: stringList(proposal.plan),
    },
    verification: {
      sandboxPassed: boolOrNull(sandbox.passed),
      report: report ? report.slice(0, REPORT_EXCERPT_LIMIT) : NOT_PROVIDED,
      riskTier: text(sandbox.risk_tier) || (riskLevel === null ? "unknown" : `L${riskLevel}`),
      votes: Object.entries(votesSummary).map(([family, verdict]) => [
        family,
        String(verdict),
      ]),
    },
    rollback: {
      pointId: improvement.rollback_point_id ?? null,
      appliedSha: improvement.applied_sha ?? null,
    },
  };
}

/**
 * Case-insensitive, trimmed identity match — the self-approval predicate.
 *
 * Fails closed on malformed input rather than throwing a raw TypeError out of
 * a render path: a non-string/undefined/null identity on either side never
 * crashes the caller — it normalizes to "", which only matches if BOTH sides
 * are empty, and callers already reject empty decided_by separately via
 * assertCanDecide.
 */
export function isSelfApproval(decidedBy: unknown, createdBy: unknown): boolean {
  // Mirrors the server-side guard (omniagentos/api/routes/improvements.py
  // _normalize_identity): NFC-normalize, trim, then case-fold before
  // comparing — a combining-mark identity and its precomposed equivalent
  // must compare equal on both sides of the boundary.
  //
  // Residual gap (Round-3 note): `.toLowerCase()` is a locale-independent-ish
  // JS approximation of Unicode casefold, not true casefold — it diverges
  // from `String.prototype.toLowerCase` for a handful of codepoints (e.g. the
  // German sharp s "ß", which casefolds to "ss" but lowercases to itself; the
  // Greek final sigma "ς"/"σ" pair) where full Unicode case-folding and
  // simple lowercasing disagree. This is defense-in-depth only — the real
  // enforcement point is the Python server guard in
  // omniagentos/api/routes/improvements.py, which is also `.casefold()`
  // (Python's own approximation, itself not a full Unicode
  // default-case-fold implementation either), so this is a display-side
  // mirror of an already-approximate check, not the source of truth.
  // R4-1: NFKC + strip format/control chars (zero-width spaces, joiners,
  // bidi marks) BEFORE emptiness/equality — keeps lockstep with the server's
  // _normalize_identity, which showed U+200B defeating both checks.
  const clean = (value: unknown): string =>
    typeof value === "string"
      ? value
          .normalize("NFKC")
          .replace(/[\p{Cf}\p{Cc}]/gu, "")
          .trim()
          .toLowerCase()
      : "";
  const a = clean(decidedBy);
  const b = clean(createdBy);
  if (!a || !b) return false;
  return a === b;
}

export type DecideAction = "approve" | "reject" | "rollback" | "pull";

/**
 * Hard governance rule (design §2.3): every decide action requires a typed,
 * non-empty decided_by identity; an approve where decided_by equals the
 * proposal author is forbidden. Rejecting your own proposal is allowed —
 * approving it is not.
 */
export function assertCanDecide(
  action: DecideAction,
  decidedBy: string,
  createdBy: string,
): void {
  // R4-1: strip invisibles before the emptiness check too — a U+200B-only
  // name must refuse, not pass as attribution that renders blank.
  const visible = decidedBy
    .normalize("NFKC")
    .replace(/[\p{Cf}\p{Cc}]/gu, "")
    .trim();
  if (!visible) {
    throw new Error(`decided_by is required to ${action} an improvement.`);
  }
  if (action === "approve" && isSelfApproval(decidedBy, createdBy)) {
    throw new Error(
      "Self-approval is not allowed: the approver must differ from the proposal author.",
    );
  }
}
