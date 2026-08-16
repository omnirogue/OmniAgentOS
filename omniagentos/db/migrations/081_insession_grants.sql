-- 081: PKG-INSESSION-FANOUT — coordinator-issued grants for in-session fan-out.
--
-- A grant authorizes ONE running swarm attempt (claude bridge session) to run
-- up to max_children of its validated subtasks as live subagents via the Task
-- tool, instead of paying a fresh CLI process per child. The PreToolUse hook
-- consumes child slots atomically (bounded by the budget, the TTL, the void
-- marker, and the attempt still being open); accounting counts live grant
-- budgets against limits.providers.<provider>.max_agents_per_account.
CREATE TABLE insession_grants (
  id               TEXT PRIMARY KEY,            -- new_id('isg')
  swarm_run_id     TEXT NOT NULL,
  board_task_id    TEXT NOT NULL,
  attempt_id       TEXT NOT NULL UNIQUE,        -- at most one grant per attempt, ever
  session_id       TEXT NOT NULL,               -- sessions.id (canonical row id)
  provider         TEXT NOT NULL,
  account_id       TEXT,                        -- claude_accounts.id when applicable
  max_children     INTEGER NOT NULL,
  children_spawned INTEGER NOT NULL DEFAULT 0,
  children_json    TEXT NOT NULL,               -- validated, NORMALIZED child specs
  created_at       TEXT NOT NULL,
  expires_at       TEXT NOT NULL,
  voided_at        TEXT,
  void_reason      TEXT
);
CREATE INDEX idx_insession_grants_session ON insession_grants(session_id);
CREATE INDEX idx_insession_grants_account ON insession_grants(account_id);
