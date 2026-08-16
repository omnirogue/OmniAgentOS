-- 124_orch_step_blocked_on_review.sql
-- orchestration_steps.status must accept 'blocked_on_review'.
--
-- Migration 044 enumerated the checkpoint's status vocabulary as
-- pending/running/done/unreviewed/denied/failed. It omitted
-- 'blocked_on_review', which orchestrator/core.py has always written for the
-- H2 outcome (the reviewer's INFRASTRUCTURE failed, so the work was never
-- judged and the run needs an operator). Because OrchestrationsDal.step_finished
-- swallows every sqlite3.Error, that write failed the CHECK and was discarded in
-- SILENCE: the step kept its previous status and resume replayed the one class
-- of step that must not be replayed unattended.
--
-- 044 is applied and checksum-frozen, so this is the forward-only fix. The
-- constraint is an inline column CHECK, so widening it requires the standard
-- SQLite table rebuild; the CHECK is WIDENED, never dropped — an unknown status
-- must still be refused. The one index on the table is dropped with it and
-- recreated verbatim. No FK references orchestration_steps; data copies verbatim
-- and nothing is backfilled (a step that lost its status to the old constraint
-- cannot be distinguished after the fact from one that legitimately holds it).

CREATE TABLE orchestration_steps_new (
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (
    status IN (
      'pending','running','done','unreviewed','denied','failed','blocked_on_review'
    )
  ),
  session_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  output_tail TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);

INSERT INTO orchestration_steps_new
  (run_id, seq, title, status, session_id, attempts, output_tail, updated_at)
SELECT run_id, seq, title, status, session_id, attempts, output_tail, updated_at
FROM orchestration_steps;

DROP TABLE orchestration_steps;
ALTER TABLE orchestration_steps_new RENAME TO orchestration_steps;

CREATE INDEX idx_orch_steps_session ON orchestration_steps(session_id) WHERE session_id IS NOT NULL;
