-- 043_longhaul.sql
-- Longhaul: long-horizon agentic coding lane (W1 schema).
--
-- Three new tables:
--   - task_categories: named, wip-limited work categories
--   - task_sessions: ordered executor-attempt chain per board task
-- And four new columns on existing tables:
--   - board_tasks: category_id, lane, park_state, longhaul_json
--   - claude_accounts: cooldown_until
--
-- All additive; existing rows untouched.

CREATE TABLE task_categories (
  id TEXT PRIMARY KEY,                       -- new_id('cat')
  name TEXT NOT NULL,                        -- display name
  slug TEXT NOT NULL UNIQUE,                 -- lower-kebab, app-computed
  color TEXT NOT NULL DEFAULT '',
  wip_limit INTEGER NOT NULL DEFAULT 1 CHECK (wip_limit >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

ALTER TABLE board_tasks ADD COLUMN category_id TEXT REFERENCES task_categories(id);
ALTER TABLE board_tasks ADD COLUMN lane TEXT CHECK (lane IN ('fast', 'longhaul'));
ALTER TABLE board_tasks ADD COLUMN park_state TEXT CHECK (park_state IN ('waiting_category', 'waiting_capacity', 'waiting_review'));
ALTER TABLE board_tasks ADD COLUMN longhaul_json TEXT NOT NULL DEFAULT '{}';
  -- {acceptance: str, max_sessions: int, workbook_path: str, review: {verdict, notes, at}, parked_detail}

CREATE TABLE task_sessions (
  id TEXT PRIMARY KEY,                       -- new_id('tks')
  board_task_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  session_id TEXT,                           -- ses_* for claude-bridge attempts; NULL for codex attempts
  harness TEXT NOT NULL CHECK (harness IN ('cli-claude', 'cli-codex')),
  model TEXT NOT NULL,
  account_id TEXT,                           -- claude_accounts.id when applicable
  started_at TEXT NOT NULL,
  ended_at TEXT,
  end_reason TEXT CHECK (end_reason IN ('completed', 'usage_limited', 'auth_failed', 'crashed', 'killed', 'unfinished_exit', 'review_denied', 'superseded')),
  detail TEXT NOT NULL DEFAULT '',
  UNIQUE(board_task_id, seq)
);
CREATE INDEX idx_task_sessions_task ON task_sessions(board_task_id, seq);
CREATE UNIQUE INDEX idx_task_sessions_live ON task_sessions(board_task_id) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX idx_task_sessions_session ON task_sessions(session_id) WHERE session_id IS NOT NULL;

ALTER TABLE claude_accounts ADD COLUMN cooldown_until TEXT;   -- UTC ISO; NULL = available

-- Indexes for category slot claiming and parking queries
CREATE INDEX idx_board_tasks_category ON board_tasks(category_id, status);
CREATE INDEX idx_board_tasks_park ON board_tasks(park_state) WHERE park_state IS NOT NULL;
