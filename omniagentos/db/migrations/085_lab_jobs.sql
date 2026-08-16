-- 085: durable lab experiment jobs.

CREATE TABLE lab_jobs (
  job_id           TEXT PRIMARY KEY,
  idempotency_key  TEXT NOT NULL UNIQUE,
  experiment_id    TEXT NOT NULL,
  state            TEXT NOT NULL CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
  dry_run          INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0,1)),
  attempt          INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  lease_owner      TEXT,
  lease_expires_at TEXT,
  lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
  checkpoint_json  TEXT NOT NULL DEFAULT '{}',
  result_json      TEXT,
  error            TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  finished_at      TEXT
);
CREATE INDEX idx_lab_jobs_claimable  ON lab_jobs(state, lease_expires_at);
CREATE INDEX idx_lab_jobs_experiment ON lab_jobs(experiment_id, created_at);
