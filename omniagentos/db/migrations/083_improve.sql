-- 083: config testing, healing, judged verdicts, and merge saga.

CREATE TABLE configtest_runs (
  run_id                 TEXT PRIMARY KEY,
  test_id                TEXT NOT NULL,
  category               TEXT NOT NULL,
  tier                   TEXT NOT NULL,
  formation_json         TEXT NOT NULL,
  models_json            TEXT NOT NULL,
  bracket_budget_json    TEXT NOT NULL,
  status                 TEXT NOT NULL CHECK (status IN ('pass', 'fail', 'timeout', 'crash', 'invalid')),
  gate_results_json      TEXT,
  wall_ms                INTEGER,
  tokens_in              INTEGER,
  tokens_out             INTEGER,
  cost_usd               REAL,
  escalation_events_json TEXT,
  lab_tournament_id      TEXT,
  created_at             TEXT NOT NULL
);

CREATE TABLE configtest_hypotheses (
  hypothesis_id        TEXT PRIMARY KEY,
  state                TEXT NOT NULL CHECK (state IN ('proposed', 'testing', 'confirmed_local', 'replicating', 'replicated', 'cross_domain', 'promotion_candidate', 'domain_local_success', 'disproved', 'dead')),
  independent_variable TEXT NOT NULL,
  evidence_json        TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);

CREATE TABLE healer_decisions (
  decision_id         TEXT PRIMARY KEY,
  failure_ref         TEXT NOT NULL,
  fixer_model         TEXT NOT NULL,
  reviewer_model      TEXT NOT NULL,
  verdict             TEXT NOT NULL CHECK (verdict IN ('confirm_upgrade', 'broken', 'reject')),
  attested_confidence REAL NOT NULL CHECK (attested_confidence >= 0.0 AND attested_confidence <= 1.0),
  rationale           TEXT NOT NULL,
  files_touched_json  TEXT NOT NULL,
  gates_evidence_json TEXT NOT NULL,
  created_at          TEXT NOT NULL
);

CREATE TABLE healer_outcomes (
  outcome_id    TEXT PRIMARY KEY,
  decision_id   TEXT NOT NULL REFERENCES healer_decisions(decision_id),
  outcome       TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

CREATE TABLE improve_verdicts (
  verdict_id         TEXT PRIMARY KEY,
  attempt_id         TEXT NOT NULL,
  tier               TEXT NOT NULL CHECK (tier IN ('T0', 'T1', 'T2')),
  judge_model        TEXT NOT NULL,
  stage              TEXT NOT NULL CHECK (stage IN ('cheap', 'premium', 'final')),
  vote               TEXT NOT NULL,
  blocker_cited      INTEGER NOT NULL DEFAULT 0 CHECK (blocker_cited IN (0, 1)),
  blocker_reproduced INTEGER NOT NULL DEFAULT 0 CHECK (blocker_reproduced IN (0, 1)),
  rationale          TEXT,
  base_sha           TEXT NOT NULL,
  head_sha           TEXT NOT NULL,
  tree_hash          TEXT NOT NULL,
  diff_hash          TEXT NOT NULL,
  judge_config_hash  TEXT NOT NULL,
  created_at         TEXT NOT NULL
);

-- On dispatcher startup, every non-terminal state (everything except VERIFIED
-- and REVERTED) must be reconcilable, and a MERGED_SHA / VERIFYING row is an
-- unverified HEAD that must be detectable by scanning state.
CREATE TABLE improve_saga (
  attempt_id      TEXT PRIMARY KEY,
  state           TEXT NOT NULL CHECK (state IN ('CALL_RESERVED', 'CALL_SENT', 'APPROVED_SHA', 'MERGE_INTENT', 'MERGED_SHA', 'VERIFYING', 'VERIFIED', 'REVERTED')),
  approved_sha    TEXT,
  merged_sha      TEXT,
  idempotency_key TEXT NOT NULL UNIQUE,
  updated_at      TEXT NOT NULL
);

CREATE TRIGGER healer_decisions_no_update
BEFORE UPDATE ON healer_decisions
BEGIN
  SELECT RAISE(ABORT, 'healer_decisions is immutable');
END;

CREATE TRIGGER healer_decisions_no_delete
BEFORE DELETE ON healer_decisions
BEGIN
  SELECT RAISE(ABORT, 'healer_decisions is immutable');
END;

CREATE TRIGGER healer_outcomes_no_update
BEFORE UPDATE ON healer_outcomes
BEGIN
  SELECT RAISE(ABORT, 'healer_outcomes is append-only');
END;

CREATE TRIGGER healer_outcomes_no_delete
BEFORE DELETE ON healer_outcomes
BEGIN
  SELECT RAISE(ABORT, 'healer_outcomes is append-only');
END;

CREATE INDEX idx_improve_verdicts_attempt ON improve_verdicts(attempt_id, created_at);
CREATE INDEX idx_improve_saga_state ON improve_saga(state, updated_at);
CREATE INDEX idx_configtest_runs_test ON configtest_runs(test_id, created_at);
CREATE INDEX idx_healer_outcomes_decision ON healer_outcomes(decision_id);
CREATE INDEX idx_configtest_hypotheses_state ON configtest_hypotheses(state, updated_at);
