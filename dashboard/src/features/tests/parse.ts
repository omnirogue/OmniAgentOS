/**
 * Pure parsers for merge-gate run receipts. No I/O.
 *
 * Real receipts drift (22–46 keys, `mode` often absent, `instrument_error`
 * sometimes a boolean). These functions must never throw: unrecognized input
 * returns null / "unclassified".
 */

export type PytestCounts = {
  failed: number | null;
  passed: number | null;
  skipped: number | null;
  warnings: number | null;
  deselected: number | null;
  durationSeconds: number | null;
};

export type RefusalClass =
  | "pass"
  | "instrument_error"
  | "mechanical_refusal"
  | "candidate_defect"
  | "unclassified";

export type ClassifyRunInput = {
  exit_code: number | null;
  instrument_error?: string | null;
  refusal_reason: string;
  steps: Array<{ name: string; status: string }>;
};

const COUNT_LABELS = {
  failed: "failed",
  passed: "passed",
  skipped: "skipped",
  warning: "warnings",
  warnings: "warnings",
  deselected: "deselected",
} as const;

type CountKey = "failed" | "passed" | "skipped" | "warnings" | "deselected";

const TOKEN_RE = /^(\d+)\s+(failed|passed|skipped|warnings?|deselected)$/i;
const DURATION_RE = /\s+in\s+(\d+(?:\.\d+)?)s(?:\s+\([^)]+\))?\s*$/;

const MECHANICAL_SUBSTRINGS = [
  "unpinned-workspace",
  "dirty-workspace",
  "signed-receipt",
  "oracle-path",
  "secrets",
  "lane-claims",
  "without an explicit verdict",
] as const;

/** Colon-prefixed step-like token, e.g. `scripts:` or `ladder(api,swarm):`. */
const STEP_COLON_RE = /[A-Za-z][\w.-]*(?:\([^)]*\))?\s*:/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function asFiniteNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const n = Number(value);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/**
 * Parse a pytest summary line such as
 * `"1 failed, 1204 passed, 1 skipped, 1 warning in 591.90s (0:09:51)"`.
 *
 * Absent tokens stay `null` (not 0). An explicit `"0 failed"` is `failed: 0`.
 * Unrecognized grammars (counterfeit counters, ruff deltas, paths, empty)
 * return `null` for the whole result.
 */
export function parsePytestSummary(detail: string): PytestCounts | null {
  try {
    if (typeof detail !== "string") return null;
    const trimmed = detail.trim();
    if (!trimmed) return null;

    const durMatch = trimmed.match(DURATION_RE);
    const durationSeconds = durMatch ? Number(durMatch[1]) : null;
    if (durMatch && !Number.isFinite(durationSeconds)) return null;

    const countsPart = (durMatch && durMatch.index !== undefined
      ? trimmed.slice(0, durMatch.index)
      : trimmed
    ).trim();
    if (!countsPart) return null;

    const tokens = countsPart.split(",").map((part) => part.trim()).filter(Boolean);
    if (tokens.length === 0) return null;

    const counts: PytestCounts = {
      failed: null,
      passed: null,
      skipped: null,
      warnings: null,
      deselected: null,
      durationSeconds,
    };

    for (const token of tokens) {
      const match = token.match(TOKEN_RE);
      if (!match) return null;
      const n = Number(match[1]);
      if (!Number.isFinite(n)) return null;
      const rawLabel = match[2].toLowerCase() as keyof typeof COUNT_LABELS;
      const key = COUNT_LABELS[rawLabel] as CountKey;
      counts[key] = n;
    }

    return counts;
  } catch {
    return null;
  }
}

function instrumentErrorPresent(value: unknown): boolean {
  if (typeof value === "string") return value.trim().length > 0;
  return false;
}

function isMechanicalRefusal(reason: string): boolean {
  const lower = reason.toLowerCase();
  return MECHANICAL_SUBSTRINGS.some((needle) => lower.includes(needle));
}

function namesFailedStep(
  reason: string,
  steps: Array<{ name: string; status: string }>,
): boolean {
  const lower = reason.toLowerCase();
  for (const step of steps) {
    if (!step.name || step.status === "ok") continue;
    if (lower.includes(step.name.toLowerCase())) return true;
  }
  return /\bfailed\b/i.test(reason) && STEP_COLON_RE.test(reason);
}

function normalizeSteps(raw: unknown): Array<{ name: string; status: string }> {
  if (!Array.isArray(raw)) return [];
  const out: Array<{ name: string; status: string }> = [];
  for (const item of raw) {
    if (!isRecord(item)) continue;
    out.push({
      name: asString(item.name),
      status: asString(item.status),
    });
  }
  return out;
}

/**
 * Classify a gate run. Priority (first match wins), matching DESIGN.md §1:
 *   1. exit_code === 0 → pass
 *   2. non-empty instrument_error → instrument_error
 *   3. mechanical taxonomy substring → mechanical_refusal
 *   4. refusal names a non-ok step, or `failed` + colon-prefixed step token
 *      → candidate_defect
 *   5. else → unclassified
 *
 * `mode` is display-only and is not read here.
 */
export function classifyRun(run: ClassifyRunInput): RefusalClass {
  try {
    if (!isRecord(run)) return "unclassified";

    const exitCode = asFiniteNumber(run.exit_code);
    if (exitCode === 0) return "pass";

    if (instrumentErrorPresent(run.instrument_error)) return "instrument_error";

    const reason = asString(run.refusal_reason);
    if (isMechanicalRefusal(reason)) return "mechanical_refusal";

    const steps = normalizeSteps(run.steps);
    if (reason && namesFailedStep(reason, steps)) return "candidate_defect";

    return "unclassified";
  } catch {
    return "unclassified";
  }
}
