-- 072_longhaul_provider_harnesses.sql
-- S14D / L14-owned forward repair for L-14.
--
-- Migration 043 is immutable and SQLite cannot ALTER a CHECK constraint.
-- Rebuild task_sessions with the exact 043 shape, widening only harness so
-- longhaul can durably route the already-supported Grok/Gemini/Kimi adapters.

CREATE TABLE task_sessions_072 (
  id TEXT PRIMARY KEY,
  board_task_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  session_id TEXT,
  harness TEXT NOT NULL CHECK (
    harness IN ('cli-claude', 'cli-codex', 'cli-grok', 'cli-gemini', 'cli-kimi')
  ),
  model TEXT NOT NULL,
  account_id TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  end_reason TEXT CHECK (
    end_reason IN (
      'completed', 'usage_limited', 'auth_failed', 'crashed', 'killed',
      'unfinished_exit', 'review_denied', 'superseded'
    )
  ),
  detail TEXT NOT NULL DEFAULT '',
  UNIQUE(board_task_id, seq)
);

INSERT INTO task_sessions_072 (
  id,
  board_task_id,
  seq,
  session_id,
  harness,
  model,
  account_id,
  started_at,
  ended_at,
  end_reason,
  detail
)
SELECT
  id,
  board_task_id,
  seq,
  session_id,
  harness,
  model,
  account_id,
  started_at,
  ended_at,
  end_reason,
  detail
FROM task_sessions;

DROP TABLE task_sessions;
ALTER TABLE task_sessions_072 RENAME TO task_sessions;

CREATE INDEX idx_task_sessions_task
  ON task_sessions(board_task_id, seq);
CREATE UNIQUE INDEX idx_task_sessions_live
  ON task_sessions(board_task_id) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX idx_task_sessions_session
  ON task_sessions(session_id) WHERE session_id IS NOT NULL;
