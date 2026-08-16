-- 045_swarm.sql
-- Swarm Mode (WP1 schema): parallel multi-agent execution over the task board.
--
-- Three new tables:
--   - swarm_runs: one row per swarm run (plan, concurrency, budget, lifecycle)
--   - swarm_deps: DAG edges between board tasks inside a run
--   - swarm_attempts: ordered executor-attempt chain per swarm board task
-- And four new columns on existing tables:
--   - board_tasks: swarm_run_id, swarm_json
--       (the lane CHECK from 043 is FROZEN at ('fast','longhaul') — swarm
--        membership is swarm_run_id ONLY; lane stays NULL on swarm cards)
--   - sessions: account_id (shared inflight attribution across every lane —
--       swarm's durable inflight must see longhaul/fast claude sessions too)
--   - claude_accounts: provider (generalized multi-provider account pool;
--       existing rows backfill to 'claude' via the column default)
--
-- All additive; existing rows untouched.

CREATE TABLE swarm_runs (
  id                 TEXT PRIMARY KEY,           -- new_id('swr')
  board_task_id      TEXT,                       -- originating board card, if any
  project_id         TEXT,
  working_dir        TEXT NOT NULL DEFAULT '',
  goal               TEXT NOT NULL DEFAULT '',
  status             TEXT NOT NULL DEFAULT 'queued' CHECK (
    status IN ('queued', 'planning', 'running', 'merging', 'completed', 'failed', 'cancelled')
  ),                                             -- 'queued' = fleet admission control
  plan_json          TEXT NOT NULL DEFAULT '{}', -- SwarmPlan (tasks, deps, assumptions)
  target_concurrency INTEGER NOT NULL DEFAULT 1,
  max_concurrency    INTEGER NOT NULL DEFAULT 10,
  budget_usd_max     REAL,
  cost_usd           REAL NOT NULL DEFAULT 0,
  metrics_json       TEXT NOT NULL DEFAULT '{}',
  summary_note_path  TEXT,
  error              TEXT,
  heartbeat_at       TEXT,                       -- coordinator lease (queued runs have no coordinator)
  started_at         TEXT,
  finished_at        TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);
CREATE INDEX idx_swarm_runs_status ON swarm_runs(status);

CREATE TABLE swarm_deps (
  swarm_run_id       TEXT NOT NULL,              -- swarm_runs.id
  task_id            TEXT NOT NULL,              -- board_tasks.id (the dependent)
  depends_on_task_id TEXT NOT NULL,              -- board_tasks.id (the prerequisite)
  PRIMARY KEY (task_id, depends_on_task_id)
);
CREATE INDEX idx_swarm_deps_run ON swarm_deps(swarm_run_id);

CREATE TABLE swarm_attempts (
  id            TEXT PRIMARY KEY,                -- new_id('swa')
  swarm_run_id  TEXT NOT NULL,
  board_task_id TEXT NOT NULL,
  seq           INTEGER NOT NULL,
  session_id    TEXT,                            -- ses_* when a sessions row exists
  provider      TEXT NOT NULL,                   -- claude | codex | grok | gemini | kimi
  model         TEXT NOT NULL,
  tier          TEXT,
  account_id    TEXT,                            -- claude_accounts.id when applicable
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  end_reason    TEXT CHECK (end_reason IN (
    'completed', 'review_denied', 'rate_limited', 'auth_failed', 'crashed',
    'timeout', 'killed', 'split', 'rerouted', 'blocked', 'budget'
  )),
  detail        TEXT NOT NULL DEFAULT '',
  UNIQUE(board_task_id, seq)
);
CREATE INDEX idx_swarm_attempts_run ON swarm_attempts(swarm_run_id);
-- Mirror of 043's idx_task_sessions_live: at most ONE live attempt per task.
CREATE UNIQUE INDEX idx_swarm_attempts_live ON swarm_attempts(board_task_id)
  WHERE ended_at IS NULL;

ALTER TABLE board_tasks ADD COLUMN swarm_run_id TEXT;  -- swarm_runs.id; lane stays NULL
ALTER TABLE board_tasks ADD COLUMN swarm_json TEXT NOT NULL DEFAULT '{}';
  -- {complexity, risk_class, est_manual_minutes, est_agent_minutes, owned_paths,
  --  tier_hint, acceptance, verify_command, integration: bool, swarm_phase}

ALTER TABLE sessions ADD COLUMN account_id TEXT;       -- claude_accounts.id

ALTER TABLE claude_accounts ADD COLUMN provider TEXT NOT NULL DEFAULT 'claude';
  -- claude | codex | grok | gemini | kimi; existing rows backfill 'claude' via default

-- Durable inflight accounting (COUNT sessions by account in live states).
CREATE INDEX idx_sessions_account_state ON sessions(account_id, state);
-- Run membership lookup; partial: almost every board card is NOT a swarm card.
CREATE INDEX idx_board_tasks_swarm ON board_tasks(swarm_run_id)
  WHERE swarm_run_id IS NOT NULL;
