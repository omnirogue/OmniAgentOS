import type { PytestCounts, RefusalClass } from "./parse";

export type { PytestCounts, RefusalClass };

export type GateStep = {
  name: string;
  status: string;
  duration_s: number | null;
  counts: PytestCounts | null;
  detailRaw: string;
};

export type GateRun = {
  candidate_sha: string;
  shortSha: string;
  branch: string;
  mode: string | null;
  exit_code: number | null;
  refusal_reason: string;
  instrument_error: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_s: number | null;
  steps: GateStep[];
  refusal_class: RefusalClass;
};

export type GateRunError = {
  file: string;
  error: string;
};

export type CiRun = {
  status: string;
  conclusion: string;
  workflowName: string;
  createdAt: string;
  headBranch: string;
  headSha: string;
};

export type LandingDay = {
  date: string;
  count: number;
};

export type GateRunsSection = {
  runs: GateRun[];
  errors: GateRunError[];
};

export type CiSection = { runs: CiRun[] } | { error: string };
export type LandingsSection =
  | { days: LandingDay[]; todayCount: number }
  | { error: string };

export type TestsPayload = {
  generatedAt: string;
  gateRuns: GateRunsSection;
  ci: CiSection;
  landings: LandingsSection;
};

export type TestsErrorPayload = {
  error: string;
};
