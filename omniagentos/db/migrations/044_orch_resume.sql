-- 044_orch_resume.sql
-- Durable orchestration checkpoints, conductor claims, resume, and bounded retry.
--
-- All additive; existing rows receive backward-compatible defaults.

ALTER TABLE orchestrations ADD COLUMN goal TEXT NOT NULL DEFAULT '';
ALTER TABLE orchestrations ADD COLUMN plan_json TEXT;
ALTER TABLE orchestrations ADD COLUMN params_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE orchestrations ADD COLUMN conductor_pid INTEGER;
ALTER TABLE orchestrations ADD COLUMN conductor_claimed_at TEXT;
ALTER TABLE orchestrations ADD COLUMN resume_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE orchestrations ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;

CREATE TABLE orchestration_steps (
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','done','unreviewed','denied','failed')),
  session_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  output_tail TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE INDEX idx_orch_steps_session ON orchestration_steps(session_id) WHERE session_id IS NOT NULL;
